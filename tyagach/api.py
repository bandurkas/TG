"""Tyagach read API + pause/close-all control. Read-only against the same
SQLite file the loop writes to (WAL mode allows concurrent readers).
Listens on 0.0.0.0:8100 — open port, no auth, matching opt-app's existing
exposure pattern (see ARCHITECTURE.md: accepted, not hardened)."""
from __future__ import annotations

import os

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from db import repo
from services import market_data

app = FastAPI(title="Tyagach API")
app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_methods=["*"], allow_headers=["*"])

# Idempotent (INSERT OR IGNORE) — guards against this process winning the race
# to query bot_state before the loop container's own init_db() has run, since
# docker-compose's depends_on only waits for container start, not for that.
repo.init_db(float(os.environ.get("TYAGACH_STARTING_BALANCE", "2000")))


def _max_dd_pct(history: list[dict], start_balance: float) -> float:
    peak = start_balance if start_balance > 0 else 0.0
    max_dd = 0.0
    for pt in history:
        bal = pt["balance_usdt"]
        peak = max(peak, bal)
        if peak > 0:
            max_dd = max(max_dd, (peak - bal) / peak)
    return round(max_dd * 100, 2)


@app.get("/api/v1/tyagach/state")
def get_state():
    state = repo.get_state()
    open_positions = repo.get_open_positions()
    stats = repo.get_position_stats()
    start_balance = state.get("start_balance_usdt") or 0.0
    history = repo.get_equity_history(limit=5000)
    return {
        "balance_usdt": state.get("balance_usdt"),
        "start_balance_usdt": start_balance,
        "started_at_ms": state.get("started_at_ms"),
        "paused": bool(state.get("paused")),
        "last_processed_ts_ms": state.get("last_processed_ts_ms"),
        "open_position_count": len(open_positions),
        "n_closed": stats["n_closed"],
        "wins": stats["wins"],
        "losses": stats["losses"],
        "win_rate": stats["win_rate"],
        "realized_usd": stats["realized_usd"],
        "max_dd_pct": _max_dd_pct(history, start_balance),
    }


@app.get("/api/v1/tyagach/positions")
def get_positions(status: str | None = None, limit: int = 200):
    return repo.get_positions(status=status, limit=limit)


@app.get("/api/v1/tyagach/chart")
def get_chart(kline_limit: int = 288):
    """Candlestick data + open positions' entry/stop/tp SPOT price levels.
    Unlike the straddle bots' /chart (which back-solves an option-premium SL
    into an approximate spot level), Tyagach's stop_price/tp_price ARE spot
    levels already — the R-multiple system operates directly on price, so no
    back-solving is needed here."""
    klines = market_data.get_klines(limit=kline_limit)
    spot = klines[-1]["close"] if klines else None
    open_positions = repo.get_open_positions()
    zones = [
        {
            "id": p["id"],
            "zone_kind": p["zone_kind"],
            "direction": p["direction"],
            "option_side": p["option_side"],
            "symbol": p["symbol"],
            "strike": p["strike"],
            "entry_spot": p["entry_spot"],
            "stop_price": p["stop_price"],
            "tp_price": p["tp_price"],
        }
        for p in open_positions
    ]
    return {
        "spot": spot,
        "klines": [
            {"start_ms": k["ts_ms"], "open": k["open"], "high": k["high"], "low": k["low"],
             "close": k["close"], "volume": k["volume"]}
            for k in klines
        ],
        "zones": zones,
    }


@app.get("/api/v1/tyagach/equity_history")
def get_equity_history(limit: int = 1000):
    return repo.get_equity_history(limit=limit)


@app.post("/api/v1/tyagach/pause")
def pause():
    repo.set_paused(True)
    return {"paused": True}


@app.post("/api/v1/tyagach/resume")
def resume():
    repo.set_paused(False)
    return {"paused": False}


@app.post("/api/v1/tyagach/close_all")
def close_all():
    """Pauses new entries and invalidates all pending zone signals. Does NOT
    flatten real open option positions — that needs a live Bybit call per
    position and isn't implemented yet; this is signal-level close-all only,
    matching what's safe to ship before the execution path has been
    reviewed/tested end to end."""
    repo.set_paused(True)
    n = repo.close_all_pending()
    return {"paused": True, "pending_signals_invalidated": n}
