"""Tyagach read API + pause/close-all control. Read-only against the same
SQLite file the loop writes to (WAL mode allows concurrent readers).
Listens on 0.0.0.0:8100 — open port, no auth, matching opt-app's existing
exposure pattern (see ARCHITECTURE.md: accepted, not hardened)."""
from __future__ import annotations

import os
import time

from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware

from db import repo
from services import config, execution, market_data
from services.mark_pricing import cached_quote, enrich_positions_with_mark

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
    tf_cursors = {tf: repo.get_last_processed(tf) for tf in config.ACTIVE_TFS}
    # Live equity = realized balance + unrealized PnL of open positions —
    # before 2026-07-09 the dashboard's Equity stat only moved on trade close.
    balance = state.get("balance_usdt") or 0.0
    unrealized = 0.0
    if open_positions:
        try:
            client = execution.get_client()
            enrich_positions_with_mark(open_positions, lambda sym: cached_quote(client.get_quote, sym))
            unrealized = sum(r.get("unrealized_pnl_usd") or 0.0 for r in open_positions)
        except execution.ExecutionError as e:
            print(f"[api] state mark enrichment skipped, no execution client: {e}", flush=True)
    return {
        "balance_usdt": state.get("balance_usdt"),
        "equity_usd": balance + unrealized,
        "unrealized_usd": unrealized,
        "start_balance_usdt": start_balance,
        "started_at_ms": state.get("started_at_ms"),
        "paused": bool(state.get("paused")),
        "last_processed_ts_ms": state.get("last_processed_ts_ms"),
        "tf_cursors": tf_cursors,
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
    rows = repo.get_positions(status=status, limit=limit)
    try:
        client = execution.get_client()
    except execution.ExecutionError as e:
        print(f"[api] positions mark enrichment skipped, no execution client: {e}", flush=True)
        for r in rows:
            r.setdefault("current_mark_usd", None)
            r.setdefault("unrealized_pnl_usd", None)
        return rows
    return enrich_positions_with_mark(rows, lambda sym: cached_quote(client.get_quote, sym))


# /chart TTL cache: the dashboard polls every 15s per open tab; without this
# every poll would hit Bybit for the forming bar (rate-limit risk — 10006
# already observed on this host 2026-07-02 00:00).
_chart_cache: dict = {"ts": 0.0, "klines": []}


@app.get("/api/v1/tyagach/chart")
def get_chart(kline_limit: int = 288):
    """Candlestick data + open positions' entry/stop/tp SPOT price levels.
    Unlike the straddle bots' /chart (which back-solves an option-premium SL
    into an approximate spot level), Tyagach's stop_price/tp_price ARE spot
    levels already — the R-multiple system operates directly on price, so no
    back-solving is needed here.

    Closed bars come from db.repo (written by the loop process, see
    market_data.get_latest_forming_bar's docstring for why api no longer
    fetches its own closed-bar history from Bybit); only the still-forming
    bar is fetched directly here so the chart ticks between 15m closes
    instead of freezing for a full bar width."""
    import time as _t
    now = _t.time()
    if now - _chart_cache["ts"] > 25:
        closed = repo.get_klines("15m", 1000)
        fresh = market_data.get_latest_forming_bar("15m", 2)
        merged = {b["ts_ms"]: b for b in closed}
        for b in fresh:
            merged[b["ts_ms"]] = b
        _chart_cache.update(ts=now, klines=sorted(merged.values(), key=lambda b: b["ts_ms"])[-1000:])
    klines = _chart_cache["klines"][-kline_limit:]
    spot = klines[-1]["close"] if klines else None
    open_positions = repo.get_open_positions()
    zones = [
        {
            "id": p["id"],
            "timeframe": p.get("timeframe", "15m"),
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
    """Pauses new entries, invalidates pending zone signals, AND flags every
    open position for buyback. The LOOP executes the actual closes on its
    next tick (~POLL_SECONDS) so position writes stay with the single writer
    (see loop.py's _close_all_now)."""
    repo.set_paused(True)
    n = repo.close_all_pending()
    repo.request_close_all()
    return {"paused": True, "pending_signals_invalidated": n,
            "note": "loop will buy back all open positions on next tick"}


@app.post("/api/v1/tyagach/close_position/{pos_id}")
def close_position(pos_id: int):
    """Partial close: queues a single-position buyback for the loop to
    execute on its next tick (~POLL_SECONDS), same single-writer pattern as
    /close_all but scoped to one position and WITHOUT pausing the bot. 404s
    immediately if the position isn't currently open, so the dashboard
    doesn't show a stale "closing…" state for one that already resolved."""
    if repo.get_open_position(pos_id) is None:
        raise HTTPException(status_code=404, detail=f"position {pos_id} not open")
    repo.request_close_position(pos_id, int(time.time() * 1000))
    return {"ok": True, "note": "loop will buy back this position on next tick"}
