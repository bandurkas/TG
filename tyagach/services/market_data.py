"""Live market data: ETH spot/klines from Bybit (public, no auth — mirrors
opt-app's bybit_client.py convention of testnet=False for market data, since
Bybit's options testnet has thin/no real chain liquidity and spot/kline data
should reflect the real market regardless of where orders execute), and ETH
DVOL from Deribit (public, works directly from any host unlike Bybit's API
which the Mac blocks — this runs on VPS3 so that restriction doesn't apply,
but the public Deribit endpoint is used either way)."""
from __future__ import annotations

import time

import requests
from pybit.unified_trading import HTTP

from . import config

_public_session = HTTP(testnet=False)

DERIBIT_VOL_URL = "https://www.deribit.com/api/v2/public/get_volatility_index_data"


def get_klines(limit: int = config.ROLLING_WINDOW_BARS) -> list[dict]:
    """Returns up to `limit` most recent CLOSED 15m candles, oldest->newest.
    Bybit's kline list always includes the current still-forming candle as
    list[0] (newest first) — callers must drop it before treating the last
    row as a closed bar; see `loop.py`'s use of this."""
    out: list[dict] = []
    end = None
    remaining = limit
    while remaining > 0:
        batch = min(1000, remaining)
        params = {"category": "linear", "symbol": config.SYMBOL, "interval": config.KLINE_INTERVAL,
                   "limit": batch}
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
    out.sort(key=lambda c: c["ts_ms"])
    # de-dup in case of overlapping pages
    seen = set()
    dedup = []
    for c in out:
        if c["ts_ms"] in seen:
            continue
        seen.add(c["ts_ms"])
        dedup.append(c)
    return dedup[-limit:]


def get_spot_price() -> float:
    resp = _public_session.get_tickers(category="linear", symbol=config.SYMBOL)
    return float(resp["result"]["list"][0]["lastPrice"])


def get_latest_dvol() -> float | None:
    """Latest ETH DVOL (annualized %, e.g. 65.0 means 65%), or None on error.
    Queries the last 2 hourly bars and takes the most recent close."""
    now_ms = int(time.time() * 1000)
    start_ms = now_ms - 2 * 3600 * 1000
    params = {"currency": config.BASE_COIN, "start_timestamp": start_ms, "end_timestamp": now_ms,
              "resolution": 3600}
    try:
        resp = requests.get(DERIBIT_VOL_URL, params=params, timeout=15)
        resp.raise_for_status()
        data = resp.json()["result"]["data"]
        if not data:
            return None
        # each row: [timestamp, open, high, low, close]
        return float(sorted(data, key=lambda r: r[0])[-1][4])
    except Exception as e:  # noqa: BLE001
        print(f"[market_data] DVOL fetch error: {e}", flush=True)
        return None
