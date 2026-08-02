"""Live market data: ETH spot/klines from Bybit (public, no auth — mirrors
opt-app's bybit_client.py convention of testnet=False for market data, since
Bybit's options testnet has thin/no real chain liquidity and spot/kline data
should reflect the real market regardless of where orders execute), and ETH
DVOL from Deribit (public, works directly from any host unlike Bybit's API
which the Mac blocks — this runs on VPS3 so that restriction doesn't apply,
but the public Deribit endpoint is used either way).

Kline caching strategy (to kill the ErrCode 10006 rate-limit spam):
  - Startup: one full backfill per TF (ROLLING_WINDOW bars), paginated.
  - Steady state: fetch only the last ~50 bars, merge & trim.
  - A TF is only fetched when its next scheduled bar could have closed,
    so a 2h TF costs ~12 fetches/day instead of ~1440.
"""
from __future__ import annotations

import time
from typing import Optional

import requests
from pybit.unified_trading import HTTP

from . import config

_public_session = HTTP(testnet=False)

DERIBIT_VOL_URL = "https://www.deribit.com/api/v2/public/get_volatility_index_data"

# Per-TF kline cache: {tf_label: sorted list of bar dicts (oldest→newest)}
_kline_cache: dict[str, list[dict]] = {}

# Timestamp of the latest closed bar we expect next, per TF.
# None = unknown (will be computed after the first fetch).
_next_bar_due: dict[str, Optional[int]] = {}

# DVOL in-process cache (60 s TTL): DVOL is hourly data, identical across all
# TFs — no point making 4 Deribit requests per tick when multi-TF fires.
_dvol_last_value: Optional[float] = None
_dvol_last_ts: float = 0.0


def _fetch_klines_paged(interval: str, limit: int, end_ms: int | None = None) -> list[dict]:
    """Paginated Bybit kline fetch.  Returns up to `limit` bars, oldest→newest,
    INCLUDING the still-forming current bar if the caller didn't page far enough
    back to exclude it (Bybit includes it as list[0] on an unbounded/latest
    query) — callers that must not persist a forming bar's OHLC need to run
    the result through `_closed_bars` themselves; this function only paginates
    and dedups, it does not know "now"."""
    out: list[dict] = []
    end = end_ms
    remaining = limit
    while remaining > 0:
        batch = min(1000, remaining)
        params: dict = {"category": "linear", "symbol": config.SYMBOL,
                        "interval": interval, "limit": batch}
        if end is not None:
            params["end"] = end
        resp = _public_session.get_kline(**params)
        rows = resp["result"]["list"]
        if not rows:
            break
        for r in reversed(rows):
            out.append({
                "ts_ms": int(r[0]), "open": float(r[1]), "high": float(r[2]),
                "low": float(r[3]), "close": float(r[4]), "volume": float(r[5]),
            })
        end = int(rows[-1][0]) - 1
        remaining -= len(rows)
        if len(rows) < batch:
            break

    # de-dup and sort oldest→newest
    seen: set[int] = set()
    dedup = []
    for c in sorted(out, key=lambda c: c["ts_ms"]):
        if c["ts_ms"] not in seen:
            seen.add(c["ts_ms"])
            dedup.append(c)
    return dedup


def _merge_into_cache(tf: str, new_bars: list[dict]) -> None:
    """Union-merge `new_bars` into the persistent per-TF cache, keyed by
    ts_ms. Last-write-wins on a ts_ms collision (a later fetch's OHLC for a
    given bar always supersedes an earlier one) — NOT skip-if-present. This
    matters because a bar can legitimately be re-fetched: `get_klines`'s
    incremental path (unlike its cold-start path) does not filter out the
    still-forming bar before calling this, by design (see get_klines) it MAY
    pass a not-yet-closed bar through. If merge ever skipped already-seen
    ts_ms, that forming bar's partial OHLC (captured seconds into its life,
    understating true high/low) would be locked in forever once real time
    caught up and `_closed_bars` started returning it — every later fetch's
    correct, final OHLC for that same ts_ms would be silently dropped since
    the key already existed. Overwrite makes the cache self-healing instead:
    whatever we fetch last for a given ts_ms is authoritative.
    Regression: see tests/test_market_data.py (2026-08-02 finding — this
    exact bug meant nearly every live bar past the first cold-start window
    was permanently frozen at its ~0-60s-old forming snapshot, corrupting
    the high/low range that swing/OB/FVG detection depends on; zone
    formation on live data collapsed from ~5-19/day to ~0-2/day within days
    of the last container restart while the same detector on a fresh
    re-fetch of the identical real price history found the normal rate)."""
    merged_by_ts = {c["ts_ms"]: c for c in _kline_cache.get(tf, [])}
    for b in new_bars:
        merged_by_ts[b["ts_ms"]] = b
    merged = sorted(merged_by_ts.values(), key=lambda c: c["ts_ms"])
    tf_cfg = config.TIMEFRAMES[tf]
    _kline_cache[tf] = merged[-tf_cfg.rolling_window:]


def _closed_bars(tf: str, bars: list[dict]) -> list[dict]:
    """Strip the still-forming bar (ts_ms + bar_ms > now)."""
    bar_ms = config.TIMEFRAMES[tf].bar_ms
    now_ms = int(time.time() * 1000)
    return [b for b in bars if b["ts_ms"] + bar_ms <= now_ms]


def get_klines(tf: str = "15m") -> list[dict]:
    """Return the full rolling-window of CLOSED bars for `tf`, oldest→newest.

    First call per TF does a full paginated backfill.  Subsequent calls fetch
    only ~50 recent bars and merge, then return the trimmed cache.  The caller
    still sees a complete window every time.

    Both the cold-start and incremental paths filter through `_closed_bars`
    BEFORE the result is written into `_kline_cache` (not just on read) —
    the persistent cache must never contain a bar that was still forming at
    fetch time, since `_merge_into_cache` treats whatever it's given as a
    keeper. See `_merge_into_cache`'s docstring for what went wrong when the
    incremental path skipped this filter (2026-08-02)."""
    tf_cfg = config.TIMEFRAMES[tf]

    if tf not in _kline_cache:
        # Cold start: full backfill
        bars = _fetch_klines_paged(tf_cfg.interval, tf_cfg.rolling_window)
        _kline_cache[tf] = _closed_bars(tf, bars)
        return _kline_cache[tf]

    now_ms = int(time.time() * 1000)
    due = _next_bar_due.get(tf)
    if due is not None and now_ms < due:
        # No new bar possible yet — return cached data without a network call
        return _closed_bars(tf, _kline_cache[tf])

    # Incremental update: fetch only the last 50 bars, but only PERSIST the
    # ones that are genuinely closed — the freshest entry Bybit returns here
    # is almost always the still-forming current bar, and merging it as-is
    # would freeze its ~0-60s-old partial OHLC into the cache forever (see
    # _merge_into_cache docstring).
    fresh = _fetch_klines_paged(tf_cfg.interval, 50)
    _merge_into_cache(tf, _closed_bars(tf, fresh))

    closed = _closed_bars(tf, _kline_cache[tf])

    # Schedule next fetch: last closed bar's ts + 2 bar widths (one full bar + buffer)
    if closed:
        last_ts = closed[-1]["ts_ms"]
        _next_bar_due[tf] = last_ts + 2 * tf_cfg.bar_ms
    return closed


def get_latest_forming_bar(tf: str = "15m", n: int = 2) -> list[dict]:
    """Direct, cache-free Bybit fetch of the freshest `n` bars (may include
    the still-forming bar) — for the dashboard chart only, merged by the
    caller on top of closed bars read from db.repo (the loop-written shared
    cache). Deliberately bypasses `_kline_cache`/`get_klines()`: before
    2026-08-02 the api process ran its own independent copy of that cache
    (its own cold-start backfill + incremental polling), doubling kline
    fetch volume against the same Bybit rate limit the loop was already
    hitting. This function makes exactly one small request and holds no
    state; the caller must TTL-cache the result (api.py's /chart does, 25s)."""
    return _fetch_klines_paged(config.TIMEFRAMES[tf].interval, n)


def get_spot_price() -> float:
    resp = _public_session.get_tickers(category="linear", symbol=config.SYMBOL)
    return float(resp["result"]["list"][0]["lastPrice"])


def get_latest_dvol() -> float | None:
    """Latest ETH DVOL (annualized %, e.g. 65.0 means 65%), or None on error.
    Queries the last 2 hourly bars and takes the most recent close.
    Results are cached for 60 s — DVOL is hourly, and the multi-TF loop may
    call this once per TF per tick; no point making 4 identical Deribit calls."""
    global _dvol_last_value, _dvol_last_ts
    now = time.time()
    if now - _dvol_last_ts < 60.0:
        return _dvol_last_value

    now_ms = int(now * 1000)
    start_ms = now_ms - 2 * 3600 * 1000
    params = {"currency": config.BASE_COIN, "start_timestamp": start_ms, "end_timestamp": now_ms,
              "resolution": 3600}
    try:
        resp = requests.get(DERIBIT_VOL_URL, params=params, timeout=15)
        resp.raise_for_status()
        data = resp.json()["result"]["data"]
        result = float(sorted(data, key=lambda r: r[0])[-1][4]) if data else None
    except Exception as e:  # noqa: BLE001
        print(f"[market_data] DVOL fetch error: {e}", flush=True)
        result = None
    _dvol_last_value = result
    _dvol_last_ts = now
    return result
