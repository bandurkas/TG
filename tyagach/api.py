"""Tyagach read API + pause/close-all control. Read-only against the same
SQLite file the loop writes to (WAL mode allows concurrent readers).
Listens on 0.0.0.0:8100 — open port, no auth, matching opt-app's existing
exposure pattern (see ARCHITECTURE.md: accepted, not hardened)."""
from __future__ import annotations

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from db import repo

app = FastAPI(title="Tyagach API")
app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_methods=["*"], allow_headers=["*"])


@app.get("/api/v1/tyagach/state")
def get_state():
    state = repo.get_state()
    open_positions = repo.get_open_positions()
    return {
        "balance_usdt": state.get("balance_usdt"),
        "paused": bool(state.get("paused")),
        "last_processed_ts_ms": state.get("last_processed_ts_ms"),
        "open_position_count": len(open_positions),
    }


@app.get("/api/v1/tyagach/positions")
def get_positions(status: str | None = None, limit: int = 200):
    return repo.get_positions(status=status, limit=limit)


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
