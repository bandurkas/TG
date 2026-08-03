"""Tyagach main loop. Wakes every config.POLL_SECONDS. Each wake:
  1. Real-expiry sweep — close any open position past its instrument expiry,
     regardless of TF cadence (so nothing is held past the actual option expiry).
  2. Realtime exit sweep — check SL/TP for ALL open positions against the
     live spot price, regardless of TF cadence (so a position can't sit past
     its TP/SL for up to a full bar width waiting on its own TF to close).
  3. For each active TF whose bar(s) have closed since its cursor:
       a. sync_new_zones + scan_pending_zones on the rolling window.
       b. Walk new bars chronologically: check SL/TP for that TF's positions
          (redundant with #2 in the common case, kept as a safety net + to
          match how each cell was backtested), then evaluate entry signals.
       c. Advance the TF cursor.

Per-TF sub-books (architecture decision A): same-direction conflict and
per-zone caps are evaluated within the TF; sizing draws from the shared
balance; a global slot/margin ceiling prevents over-leveraging.

Real Bybit mainnet orders via services/execution.py; SQLite state via
db/repo.py.  Paper mode (default) simulates fills with a corrected realistic
fee model (0.03% of underlying notional, cap 12.5% of premium)."""
from __future__ import annotations

import os
import sys
import time

from db import repo
from services import config, execution, market_data, portfolio_state, signal_engine, telegram_notify
from services.mark_pricing import cached_quote, enrich_positions_with_mark

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "core"))
import bs_pricer  # noqa: E402  (fill sanity floor in _execute_open)

STARTING_BALANCE = float(os.environ.get("TYAGACH_STARTING_BALANCE", "2000"))

# Periodic equity snapshot cadence. Before 2026-07-09 equity_snapshots was only
# written on trade close (set_balance) — 21 points in 2 weeks, so the dashboard
# curve flatlined for days between closes and never showed unrealized PnL.
EQUITY_SNAPSHOT_EVERY_MS = 10 * 60 * 1000


def _snapshot_equity(now_ms: int) -> None:
    """Write a periodic equity point = realized balance + unrealized PnL of
    open positions (marked like the API does). Quote failures degrade to the
    realized balance rather than skipping the point — a slightly stale curve
    beats a frozen one."""
    if now_ms - repo.last_equity_snapshot_ts_ms() < EQUITY_SNAPSHOT_EVERY_MS:
        return
    state = repo.get_state()
    balance = state.get("balance_usdt") or 0.0
    unrealized = 0.0
    open_rows = repo.get_open_positions()
    if open_rows:
        try:
            client = execution.get_client()
            enrich_positions_with_mark(open_rows, lambda sym: cached_quote(client.get_quote, sym))
            unrealized = sum(r.get("unrealized_pnl_usd") or 0.0 for r in open_rows)
        except Exception as e:  # noqa: BLE001
            print(f"[loop] equity snapshot: mark enrichment failed, using realized only: {e!r}", flush=True)
    repo.insert_equity_snapshot(balance + unrealized, now_ms)


def _process_tf(tf: str, now_ms: int) -> None:
    """Fetch bars for `tf`, detect new zones, walk new bars for exits+entries."""
    klines = market_data.get_klines(tf)
    if not klines:
        return

    last_processed = repo.get_last_processed(tf)
    new_bars = [k for k in klines if last_processed is None or k["ts_ms"] > last_processed]
    if not new_bars:
        return
    # Persist the FULL closed-bar window `klines` (market_data's in-memory
    # view), not just `new_bars` (the delta vs the strategy's own processing
    # cursor). Those are different things: on every container restart,
    # `_kline_cache` resets and market_data re-backfills the full window from
    # Bybit, but `last_processed` survives in the DB from before the restart
    # -- so `new_bars` would only be the handful of bars closed since restart,
    # leaving `klines` (and therefore api's /chart) missing all prior history
    # until it slowly reaccumulates one bar at a time over days. Gating on
    # `new_bars` non-empty (not calling this every idle tick) keeps the write
    # cost to roughly once per bar close per TF instead of every poll.
    repo.save_klines(tf, klines)

    df = signal_engine.klines_to_df(klines)
    signal_engine.sync_new_zones(df, tf)
    triggered = signal_engine.scan_pending_zones(df, tf)

    new_bar_ts = {b["ts_ms"] for b in new_bars}
    last_new_bar_ts = new_bars[-1]["ts_ms"]
    triggered_by_bar: dict[int, list] = {}
    for e in triggered:
        # Entries whose touch bar isn't one of this tick's new bars are retries
        # of a signal that triggered earlier but never filled — route to latest.
        bucket_ts = e.entry_ts_ms if e.entry_ts_ms in new_bar_ts else last_new_bar_ts
        triggered_by_bar.setdefault(bucket_ts, []).append(e)

    current_dvol = market_data.get_latest_dvol() if triggered else None

    for bar in new_bars:
        # Exits: only check positions that belong to THIS TF's sub-book so that
        # exit logic matches how each cell was backtested (2h-OB exits on 2h bar
        # closes; 15m-OB exits on 15m bar closes).
        tf_positions = repo.get_open_positions(tf)
        if tf_positions:
            exits = portfolio_state.check_exits(tf_positions, bar["high"], bar["low"], bar["ts_ms"])
            for ex in exits:
                _execute_close(ex)  # exits always run regardless of pause

        # Entries
        bar_signals = triggered_by_bar.get(bar["ts_ms"], [])
        state = repo.get_state()
        if bar_signals and not state["paused"]:
            iv_passed = portfolio_state.filter_by_iv(bar_signals, current_dvol)
            if iv_passed:
                tf_open = repo.get_open_positions(tf)
                all_open = repo.get_open_positions()
                decisions = portfolio_state.decide_entries(
                    iv_passed, state["balance_usdt"], tf_open, all_open, current_dvol
                )
                for d in decisions:
                    _execute_open(d)

    repo.set_last_processed(tf, new_bars[-1]["ts_ms"])


_STALE_ALERTED: set[str] = set()
_STALE_FLOOR_MS = 2 * 3600 * 1000  # minimum staleness window regardless of TF cadence


def _check_stale_tfs(now_ms: int) -> None:
    """Telegram alert if a TF's processing cursor hasn't advanced within a
    generous multiple of its own bar width -- catches a loop that's alive
    (still ticking, no exceptions in the log) but silently stuck on one TF,
    e.g. Bybit returning no/stale data for that interval indefinitely. Does
    NOT catch every silent-failure shape (the 2026-08-02 kline-cache bug
    still advanced cursors fine, just on corrupted OHLC -- see
    market_data._merge_into_cache) but is cheap ops insurance against a TF
    going fully dark, which previously required a human to notice and ask.
    Dedup mirrors Jony's stuck-position-alert pattern: alert once per stall,
    clear on recovery so a later stall re-alerts."""
    for tf in config.ACTIVE_TFS:
        last = repo.get_last_processed(tf)
        if last is None:
            continue
        threshold = max(3 * config.TIMEFRAMES[tf].bar_ms, _STALE_FLOOR_MS)
        stale = now_ms - last > threshold
        if stale and tf not in _STALE_ALERTED:
            _STALE_ALERTED.add(tf)
            hours = (now_ms - last) / 3_600_000
            telegram_notify.notify(
                f"⚠️ <b>{tf} cursor stale</b> — no new bar processed in {hours:.1f}h. "
                f"Loop is alive but this TF may be stuck.",
            )
        elif not stale and tf in _STALE_ALERTED:
            _STALE_ALERTED.discard(tf)


def _sweep_real_expiry(now_ms: int) -> None:
    """Close any position that has passed its actual instrument expiry_ts_ms,
    regardless of which TF it belongs to and whether a new bar closed."""
    all_open = repo.get_open_positions()
    expired = portfolio_state.check_expiry_only(all_open, now_ms)
    for ex in expired:
        _execute_close(ex)


def _sweep_realtime_exits(now_ms: int) -> None:
    """Check SL/TP for ALL open positions against the LIVE spot price on
    every tick, regardless of which TF's bar last closed. Added 2026-07-04:
    the per-TF bar-close check in _process_tf only fires when THAT position's
    own TF closes a bar (e.g. once an hour for a 1h position) -- a position
    could sit well past its TP for up to a full bar width before being acted
    on (a 1h/MB position was caught ~85 min after TP with spot ~$20 past it).
    Real fills already use a live quote fetched at close time (not the bar's
    close price), so tightening the *detection* cadence doesn't retest the
    backtested edge -- entries/signal logic are untouched, this only cuts
    exit reaction latency from up to one bar width down to ~POLL_SECONDS.
    The per-TF bar-close check still runs too (kept for parity with how each
    cell was backtested, and as a safety net if a spot fetch here fails)."""
    all_open = repo.get_open_positions()
    if not all_open:
        return
    try:
        spot = market_data.get_spot_price()
    except Exception as e:  # noqa: BLE001
        print(f"[loop] realtime exit sweep: get_spot_price failed: {e!r}", flush=True)
        return
    exits = portfolio_state.check_exits(all_open, spot, spot, now_ms)
    for ex in exits:
        _execute_close(ex)


def _close_all_now() -> None:
    """Manual close-all (Mission Control button): buy back every open position
    at the live ask, same accounting path as SL/TP (_execute_close). Does NOT
    arm any circuit breaker -- a manual stop is an operator decision."""
    for p in repo.get_open_positions():
        _execute_close(portfolio_state.ExitDecision(p, "manual_close_all"))


def _close_position_now(pos_id: int) -> None:
    """Manual single-position close (Mission Control partial-close button).
    The position may already be closed by the time the loop picks up the
    request (a TP/SL/expiry sweep could have resolved it first in the same
    tick) -- that's a no-op, not an error, since get_open_position returns
    None for anything not still status='open'."""
    p = repo.get_open_position(pos_id)
    if p is None:
        return
    _execute_close(portfolio_state.ExitDecision(p, "manual_close_one"))


def _execute_open(d: portfolio_state.EntryDecision) -> None:
    e = d.entry
    cfg = config.cell_config(e.timeframe, e.kind)
    client = execution.get_client()

    instrument = client.find_instrument(d.option_side, d.strike, cfg["expiry_days"])
    if instrument is None:
        print(f"[loop] no instrument for {e.timeframe}/{e.kind} {d.option_side} "
              f"strike~{d.strike} expiry>={cfg['expiry_days']}d — skip {e.zone_key}", flush=True)
        return

    symbol = instrument["symbol"]
    quote = client.get_quote(symbol)
    if quote is None or quote["bid"] <= 0:
        print(f"[loop] no usable quote for {symbol} — skip {e.zone_key}", flush=True)
        return

    # Fill sanity floor (config.FILL_FLOOR_FRAC): a bid far below the BS-mid
    # estimate is a glitched/thin chain snapshot (live audit: median fill 85%
    # of mid, glitches at 15-54%). Skip WITHOUT consuming the signal — it stays
    # pending and retries next tick while fresh, same as the no-quote path.
    if config.FILL_FLOOR_FRAC > 0:
        strike_ = float(symbol.split("-")[2])
        T_years = max((int(instrument["deliveryTime"]) - e.entry_ts_ms) / 86_400_000, 0.01) / 365.0
        bs_mid = bs_pricer.price(d.option_side, e.entry_price, strike_, T_years, d.iv_entry / 100.0)
        if bs_mid > 0 and quote["bid"] < config.FILL_FLOOR_FRAC * bs_mid:
            print(f"[loop] fill floor: {symbol} bid={quote['bid']:.2f} < "
                  f"{config.FILL_FLOOR_FRAC:.0%} of BS-mid {bs_mid:.2f} — skip {e.zone_key}", flush=True)
            return

    qty = client.round_qty(instrument, d.num_units)
    if qty <= 0:
        print(f"[loop] sizing rounds to 0 for {e.timeframe}/{e.kind} {d.option_side} "
              f"(num_units={d.num_units}) — skip {e.zone_key}", flush=True)
        return

    # Pass entry_spot so _paper_fill can compute the correct underlying notional fee
    entry_spot = e.entry_price
    result = client.sell_to_open(symbol, qty, quote["bid"], entry_spot)
    if result is None or not result.is_filled:
        print(f"[loop] sell_to_open NOT filled for {symbol} qty={qty} — skip {e.zone_key}", flush=True)
        return

    actual_strike = float(symbol.split("-")[2])
    expiry_ms = int(instrument["deliveryTime"])
    sell_premium_received = result.avg_price * result.filled_qty
    tp_price = _tp_price(e, cfg["r_target"])

    repo.open_position(
        zone_key=e.zone_key, timeframe=e.timeframe, zone_kind=e.kind,
        direction=e.direction, option_side=d.option_side,
        symbol=symbol, strike=actual_strike, entry_ts_ms=e.entry_ts_ms,
        entry_spot=entry_spot, stop_price=e.stop_price,
        tp_price=tp_price,
        expiry_ts_ms=expiry_ms, iv_entry=d.iv_entry,
        num_units=result.filled_qty, notional=result.filled_qty * entry_spot,
        sell_premium_received=sell_premium_received, open_fee=result.fees,
        open_order_id=result.order_id,
    )
    repo.set_zone_signal_status(e.zone_key, "triggered")
    print(f"[loop] OPENED {e.timeframe}/{e.kind} {d.option_side} {symbol} "
          f"qty={result.filled_qty} premium={sell_premium_received:.2f}", flush=True)
    telegram_notify.notify_open(
        timeframe=e.timeframe, zone_kind=e.kind, option_side=d.option_side,
        symbol=symbol, strike=actual_strike,
        qty=result.filled_qty, premium_recv=sell_premium_received, fee=result.fees,
        tp_price=tp_price, stop_price=e.stop_price,
        balance_now=repo.get_state()["balance_usdt"],
    )


def _tp_price(e: signal_engine.TriggeredEntry, r_target: float) -> float:
    risk = abs(e.entry_price - e.stop_price)
    is_long = e.direction == "bullish"
    return e.entry_price + r_target * risk if is_long else e.entry_price - r_target * risk


def _execute_close(ex: portfolio_state.ExitDecision) -> None:
    p = ex.position
    client = execution.get_client()
    quote = client.get_quote(p["symbol"])
    if quote is None or quote["ask"] <= 0:
        print(f"[loop] no usable quote to close {p['symbol']} — leaving open, retry next bar",
              flush=True)
        return

    try:
        exit_spot = market_data.get_spot_price()
    except Exception as e:  # noqa: BLE001
        print(f"[loop] get_spot_price failed closing {p['symbol']}, recording 0.0: {e!r}", flush=True)
        exit_spot = 0.0

    # Use current spot for the fee calc (0.03% of underlying notional); fall
    # back to entry_spot only if the spot fetch failed (exit_spot == 0.0).
    result = client.buy_to_close(p["symbol"], p["num_units"], quote["ask"],
                                  exit_spot if exit_spot > 0 else p["entry_spot"])
    if result is None or not result.is_filled:
        print(f"[loop] buy_to_close NOT filled for {p['symbol']} — leaving open, retry next bar",
              flush=True)
        return

    buy_premium_paid = result.avg_price * result.filled_qty
    close_fee = result.fees
    gross_pnl = p["sell_premium_received"] - buy_premium_paid
    net_pnl = gross_pnl - p["open_fee"] - close_fee

    new_balance = repo.close_position_and_set_balance(
        p["id"], exit_ts_ms=int(time.time() * 1000), exit_spot=exit_spot,
        exit_reason=ex.exit_reason, close_order_id=result.order_id, pnl_net=net_pnl)
    print(f"[loop] CLOSED {p.get('timeframe','?')}/{p['symbol']} reason={ex.exit_reason} "
          f"net_pnl={net_pnl:.2f} balance={new_balance:.2f}", flush=True)
    telegram_notify.notify_close(
        symbol=p["symbol"], reason=ex.exit_reason, pnl_net=net_pnl,
        balance_after=new_balance, total_pnl_usd=new_balance - STARTING_BALANCE,
    )


def _expire_orphaned_pending_signals() -> None:
    """One-time startup sweep: expire pending zone_signals whose (tf, kind)
    is no longer in ACTIVE_CELLS. scan_pending_zones does this too, but only
    for TFs still in ACTIVE_TFS -- a TF dropped from ACTIVE_TFS entirely
    (e.g. 30m/1h, 2026-08-02 OB prune) stops ticking, so its stale pending
    rows would otherwise sit in the DB forever."""
    for row in repo.get_all_pending_zone_signals():
        if (row["timeframe"], row["kind"]) not in config.ACTIVE_CELLS:
            repo.set_zone_signal_status(row["zone_key"], "expired")


def main() -> None:
    repo.init_db(STARTING_BALANCE)
    _expire_orphaned_pending_signals()
    print(f"[loop] Tyagach starting, balance={STARTING_BALANCE}, "
          f"active_tfs={config.ACTIVE_TFS}", flush=True)

    while True:
        try:
            now_ms = int(time.time() * 1000)
            _sweep_real_expiry(now_ms)
            _sweep_realtime_exits(now_ms)

            # Mission Control "close all": API sets the flag, loop executes
            # (position writes stay with the single writer). Runs even when
            # paused -- request_close_all pauses the bot as its first step.
            if repo.pop_close_all_requested():
                print("[loop] CLOSE ALL requested — buying back all open positions", flush=True)
                _close_all_now()

            # Mission Control partial close: one or more single-position
            # requests queued via POST /close_position/{id}. Does NOT pause.
            for pos_id in repo.pop_close_requests():
                print(f"[loop] CLOSE position {pos_id} requested (manual)", flush=True)
                _close_position_now(pos_id)

            _snapshot_equity(now_ms)
            for tf in config.ACTIVE_TFS:
                try:
                    _process_tf(tf, now_ms)
                except Exception as e:  # noqa: BLE001
                    print(f"[loop] {tf} tick error: {e!r}", flush=True)
            _check_stale_tfs(now_ms)
        except Exception as e:  # noqa: BLE001
            print(f"[loop] outer tick error: {e!r}", flush=True)
        time.sleep(config.POLL_SECONDS)


if __name__ == "__main__":
    main()
