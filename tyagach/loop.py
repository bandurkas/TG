"""Tyagach main loop. Wakes every config.POLL_SECONDS, but only ACTS when a
new 15m bar has closed (all the validated entry/exit logic is bar-close
granular, matching how it was backtested — see signal_engine.py). Real
Bybit testnet orders via services/execution.py; SQLite state via db/repo.py.

Per the project's workflow (architecture -> code -> review -> test -> review
-> deploy), this file is the "code" step's deliverable — code review is
still pending before this gets run anywhere, paper or otherwise."""
from __future__ import annotations

import os
import time

from db import repo
from services import config, execution, market_data, portfolio_state, signal_engine, telegram_notify

STARTING_BALANCE = float(os.environ.get("TYAGACH_STARTING_BALANCE", "2000"))


def process_new_bar(closed_klines: list[dict], new_bars: list[dict]) -> None:
    """`closed_klines` is the full rolling window (for zone detection, which
    needs history); `new_bars` is just the bar(s) that closed since the last
    tick, in chronological order — normally exactly one, but can be several
    after a restart/outage gap. Exits are walked bar-by-bar in that order
    (so an SL/TP hit on an EARLIER gap bar isn't missed just because price
    moved away from it by the time of the latest bar), and balance/open-slot
    state is refreshed between bars so entries decided on a later bar see the
    capacity freed by an exit on an earlier one — matching how the backtest's
    `simulate()` purges exits before evaluating each new candidate."""
    df = signal_engine.klines_to_df(closed_klines)
    signal_engine.sync_new_zones(df)
    triggered = signal_engine.scan_pending_zones(df)
    new_bar_ts = {b["ts_ms"] for b in new_bars}
    last_new_bar_ts = new_bars[-1]["ts_ms"]
    triggered_by_bar: dict[int, list] = {}
    for e in triggered:
        # Entries whose touch bar isn't one of THIS tick's new bars are retries
        # of a signal that triggered earlier but never filled (still 'pending'
        # in zone_signals) — route them to the latest bar instead of losing
        # them, since their original bar will never recur as a "new" bar again.
        bucket_ts = e.entry_ts_ms if e.entry_ts_ms in new_bar_ts else last_new_bar_ts
        triggered_by_bar.setdefault(bucket_ts, []).append(e)

    current_dvol = market_data.get_latest_dvol() if triggered else None

    for bar in new_bars:
        open_positions = repo.get_open_positions()
        if open_positions:
            exits = portfolio_state.check_exits(open_positions, bar["high"], bar["low"], bar["ts_ms"])
            for ex in exits:
                _execute_close(ex)  # exits run regardless of pause — never abandon an open position

        bar_signals = triggered_by_bar.get(bar["ts_ms"], [])
        state = repo.get_state()
        if bar_signals and not state["paused"]:
            iv_passed = portfolio_state.filter_by_iv(bar_signals, current_dvol)
            if iv_passed:
                open_positions = repo.get_open_positions()
                decisions = portfolio_state.decide_entries(iv_passed, state["balance_usdt"], open_positions,
                                                            current_dvol)
                for d in decisions:
                    _execute_open(d)

    repo.set_last_processed(new_bars[-1]["ts_ms"])


def _execute_open(d: portfolio_state.EntryDecision) -> None:
    e = d.entry
    cfg = config.ZONE_CONFIG[e.kind]
    client = execution.get_client()

    instrument = client.find_instrument(d.option_side, d.strike, cfg["expiry_days"])
    if instrument is None:
        print(f"[loop] no instrument found for {e.kind} {d.option_side} strike~{d.strike} "
              f"expiry>={cfg['expiry_days']}d — skipping signal {e.zone_key}", flush=True)
        return

    symbol = instrument["symbol"]
    quote = client.get_quote(symbol)
    if quote is None or quote["bid"] <= 0:
        print(f"[loop] no usable quote for {symbol} — skipping signal {e.zone_key}", flush=True)
        return

    qty = client.round_qty(instrument, d.num_units)
    if qty <= 0:
        print(f"[loop] sizing rounds to 0 qty for {e.kind} {d.option_side} (num_units={d.num_units}, "
              f"instrument step) — skipping signal {e.zone_key}", flush=True)
        return

    result = client.sell_to_open(symbol, qty, quote["bid"])
    if result is None or not result.is_filled:
        print(f"[loop] sell_to_open NOT filled for {symbol} qty={qty} — signal {e.zone_key} not opened", flush=True)
        return

    actual_strike = float(symbol.split("-")[2])
    expiry_ms = int(instrument["deliveryTime"])
    sell_premium_received = result.avg_price * result.filled_qty

    repo.open_position(
        zone_key=e.zone_key, zone_kind=e.kind, direction=e.direction, option_side=d.option_side,
        symbol=symbol, strike=actual_strike, entry_ts_ms=e.entry_ts_ms, entry_spot=e.entry_price,
        stop_price=e.stop_price,
        tp_price=_tp_price(e, cfg["r_target"]),
        expiry_ts_ms=expiry_ms, iv_entry=d.iv_entry,
        num_units=result.filled_qty, notional=result.filled_qty * e.entry_price,
        sell_premium_received=sell_premium_received, open_fee=result.fees, open_order_id=result.order_id,
    )
    repo.set_zone_signal_status(e.zone_key, "triggered")
    print(f"[loop] OPENED {e.kind} {d.option_side} {symbol} qty={result.filled_qty} "
          f"premium={sell_premium_received:.2f}", flush=True)
    telegram_notify.notify_open(
        zone_kind=e.kind, option_side=d.option_side, symbol=symbol, strike=actual_strike,
        qty=result.filled_qty, premium_recv=sell_premium_received, fee=result.fees,
        balance_now=repo.get_state()["balance_usdt"],
    )


def _tp_price(e, r_target: float) -> float:
    risk = abs(e.entry_price - e.stop_price)
    is_long = e.direction == "bullish"
    return e.entry_price + r_target * risk if is_long else e.entry_price - r_target * risk


def _execute_close(ex: portfolio_state.ExitDecision) -> None:
    p = ex.position
    client = execution.get_client()
    quote = client.get_quote(p["symbol"])
    if quote is None or quote["ask"] <= 0:
        print(f"[loop] no usable quote to close {p['symbol']} — leaving open, will retry next bar", flush=True)
        return

    result = client.buy_to_close(p["symbol"], p["num_units"], quote["ask"])
    if result is None or not result.is_filled:
        print(f"[loop] buy_to_close NOT filled for {p['symbol']} — leaving open, will retry next bar", flush=True)
        return

    buy_premium_paid = result.avg_price * result.filled_qty
    close_fee = result.fees
    gross_pnl = p["sell_premium_received"] - buy_premium_paid
    net_pnl = gross_pnl - p["open_fee"] - close_fee

    # The exchange fill is already confirmed at this point — close_position must
    # be recorded no matter what, even if the spot-price lookup below fails.
    try:
        exit_spot = market_data.get_spot_price()
    except Exception as e:  # noqa: BLE001
        print(f"[loop] get_spot_price failed while closing {p['symbol']}, recording 0.0: {e!r}", flush=True)
        exit_spot = 0.0

    repo.close_position(p["id"], exit_ts_ms=int(time.time() * 1000), exit_spot=exit_spot,
                         exit_reason=ex.exit_reason, close_order_id=result.order_id, pnl_net=net_pnl)
    new_balance = repo.get_state()["balance_usdt"] + net_pnl
    repo.set_balance(new_balance)
    print(f"[loop] CLOSED {p['symbol']} reason={ex.exit_reason} net_pnl={net_pnl:.2f} "
          f"balance={new_balance:.2f}", flush=True)
    telegram_notify.notify_close(
        symbol=p["symbol"], reason=ex.exit_reason, pnl_net=net_pnl,
        balance_after=new_balance, total_pnl_usd=new_balance - STARTING_BALANCE,
    )


def main() -> None:
    repo.init_db(STARTING_BALANCE)
    print(f"[loop] Tyagach starting, balance={STARTING_BALANCE}", flush=True)

    while True:
        try:
            klines = market_data.get_klines()
            now_ms = int(time.time() * 1000)
            bar_ms = 15 * 60 * 1000
            closed = [k for k in klines if k["ts_ms"] + bar_ms <= now_ms]
            if closed:
                last_processed_ts_ms = repo.get_state().get("last_processed_ts_ms")
                new_bars = [k for k in closed if last_processed_ts_ms is None or k["ts_ms"] > last_processed_ts_ms]
                if new_bars:
                    process_new_bar(closed, new_bars)
        except Exception as e:  # noqa: BLE001
            print(f"[loop] tick error: {e!r}", flush=True)
        time.sleep(config.POLL_SECONDS)


if __name__ == "__main__":
    main()
