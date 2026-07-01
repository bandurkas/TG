"""Tyagach main loop. Wakes every config.POLL_SECONDS. Each wake:
  1. Real-expiry sweep — close any open position past its instrument expiry,
     regardless of TF cadence (so nothing is held past the actual option expiry).
  2. For each active TF whose bar(s) have closed since its cursor:
       a. sync_new_zones + scan_pending_zones on the rolling window.
       b. Walk new bars chronologically: check SL/TP for that TF's positions,
          then evaluate entry signals for that bar.
       c. Advance the TF cursor.

Per-TF sub-books (architecture decision A): same-direction conflict and
per-zone caps are evaluated within the TF; sizing draws from the shared
balance; a global slot/margin ceiling prevents over-leveraging.

Real Bybit mainnet orders via services/execution.py; SQLite state via
db/repo.py.  Paper mode (default) simulates fills with a corrected realistic
fee model (0.03% of underlying notional, cap 12.5% of premium)."""
from __future__ import annotations

import os
import time

from db import repo
from services import config, execution, market_data, portfolio_state, signal_engine, telegram_notify

STARTING_BALANCE = float(os.environ.get("TYAGACH_STARTING_BALANCE", "2000"))


def _process_tf(tf: str, now_ms: int) -> None:
    """Fetch bars for `tf`, detect new zones, walk new bars for exits+entries."""
    klines = market_data.get_klines(tf)
    if not klines:
        return

    last_processed = repo.get_last_processed(tf)
    new_bars = [k for k in klines if last_processed is None or k["ts_ms"] > last_processed]
    if not new_bars:
        return

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


def _sweep_real_expiry(now_ms: int) -> None:
    """Close any position that has passed its actual instrument expiry_ts_ms,
    regardless of which TF it belongs to and whether a new bar closed."""
    all_open = repo.get_open_positions()
    expired = portfolio_state.check_expiry_only(all_open, now_ms)
    for ex in expired:
        _execute_close(ex)


def _execute_open(d: portfolio_state.EntryDecision) -> None:
    e = d.entry
    cfg = config.ZONE_CONFIG[e.kind]
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

    repo.open_position(
        zone_key=e.zone_key, timeframe=e.timeframe, zone_kind=e.kind,
        direction=e.direction, option_side=d.option_side,
        symbol=symbol, strike=actual_strike, entry_ts_ms=e.entry_ts_ms,
        entry_spot=entry_spot, stop_price=e.stop_price,
        tp_price=_tp_price(e, cfg["r_target"]),
        expiry_ts_ms=expiry_ms, iv_entry=d.iv_entry,
        num_units=result.filled_qty, notional=result.filled_qty * entry_spot,
        sell_premium_received=sell_premium_received, open_fee=result.fees,
        open_order_id=result.order_id,
    )
    repo.set_zone_signal_status(e.zone_key, "triggered")
    print(f"[loop] OPENED {e.timeframe}/{e.kind} {d.option_side} {symbol} "
          f"qty={result.filled_qty} premium={sell_premium_received:.2f}", flush=True)
    telegram_notify.notify_open(
        zone_kind=e.kind, option_side=d.option_side, symbol=symbol, strike=actual_strike,
        qty=result.filled_qty, premium_recv=sell_premium_received, fee=result.fees,
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

    repo.close_position(p["id"], exit_ts_ms=int(time.time() * 1000), exit_spot=exit_spot,
                         exit_reason=ex.exit_reason, close_order_id=result.order_id, pnl_net=net_pnl)
    new_balance = repo.get_state()["balance_usdt"] + net_pnl
    repo.set_balance(new_balance)
    print(f"[loop] CLOSED {p.get('timeframe','?')}/{p['symbol']} reason={ex.exit_reason} "
          f"net_pnl={net_pnl:.2f} balance={new_balance:.2f}", flush=True)
    telegram_notify.notify_close(
        symbol=p["symbol"], reason=ex.exit_reason, pnl_net=net_pnl,
        balance_after=new_balance, total_pnl_usd=new_balance - STARTING_BALANCE,
    )


def main() -> None:
    repo.init_db(STARTING_BALANCE)
    print(f"[loop] Tyagach starting, balance={STARTING_BALANCE}, "
          f"active_tfs={config.ACTIVE_TFS}", flush=True)

    while True:
        try:
            now_ms = int(time.time() * 1000)
            _sweep_real_expiry(now_ms)
            for tf in config.ACTIVE_TFS:
                try:
                    _process_tf(tf, now_ms)
                except Exception as e:  # noqa: BLE001
                    print(f"[loop] {tf} tick error: {e!r}", flush=True)
        except Exception as e:  # noqa: BLE001
            print(f"[loop] outer tick error: {e!r}", flush=True)
        time.sleep(config.POLL_SECONDS)


if __name__ == "__main__":
    main()
