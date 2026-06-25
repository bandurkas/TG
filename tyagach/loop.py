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
from services import config, execution, market_data, portfolio_state, signal_engine

STARTING_BALANCE = float(os.environ.get("TYAGACH_STARTING_BALANCE", "2000"))


def process_new_bar(closed_klines: list[dict]) -> None:
    df = signal_engine.klines_to_df(closed_klines)
    signal_engine.sync_new_zones(df)
    triggered = signal_engine.scan_pending_zones(df)

    state = repo.get_state()
    latest_bar = closed_klines[-1]

    if triggered and not state["paused"]:
        current_dvol = market_data.get_latest_dvol()
        iv_passed = portfolio_state.filter_by_iv(triggered, current_dvol)
        if iv_passed:
            open_positions = repo.get_open_positions()
            decisions = portfolio_state.decide_entries(iv_passed, state["balance_usdt"], open_positions,
                                                        current_dvol)
            for d in decisions:
                _execute_open(d, latest_bar["ts_ms"])

    # Exits run regardless of pause — never abandon an open position.
    open_positions = repo.get_open_positions()
    if open_positions:
        exits = portfolio_state.check_exits(open_positions, latest_bar["high"], latest_bar["low"],
                                             latest_bar["ts_ms"])
        for ex in exits:
            _execute_close(ex)

    repo.set_last_processed(latest_bar["ts_ms"])


def _execute_open(d: portfolio_state.EntryDecision, now_ts_ms: int) -> None:
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
        symbol=symbol, strike=actual_strike, entry_ts_ms=now_ts_ms, entry_spot=e.entry_price,
        stop_price=e.stop_price,
        tp_price=_tp_price(e, cfg["r_target"]),
        expiry_ts_ms=expiry_ms, iv_entry=d.iv_entry,
        num_units=result.filled_qty, notional=result.filled_qty * e.entry_price,
        sell_premium_received=sell_premium_received, open_fee=result.fees, open_order_id=result.order_id,
    )
    repo.set_zone_signal_status(e.zone_key, "triggered")
    print(f"[loop] OPENED {e.kind} {d.option_side} {symbol} qty={result.filled_qty} "
          f"premium={sell_premium_received:.2f}", flush=True)


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

    repo.close_position(p["id"], exit_ts_ms=int(time.time() * 1000), exit_spot=market_data.get_spot_price(),
                         exit_reason=ex.exit_reason, close_order_id=result.order_id, pnl_net=net_pnl)
    new_balance = repo.get_state()["balance_usdt"] + net_pnl
    repo.set_balance(new_balance)
    print(f"[loop] CLOSED {p['symbol']} reason={ex.exit_reason} net_pnl={net_pnl:.2f} "
          f"balance={new_balance:.2f}", flush=True)


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
                latest_closed_ts_ms = closed[-1]["ts_ms"]
                state = repo.get_state()
                if state.get("last_processed_ts_ms") != latest_closed_ts_ms:
                    process_new_bar(closed)
        except Exception as e:  # noqa: BLE001
            print(f"[loop] tick error: {e!r}", flush=True)
        time.sleep(config.POLL_SECONDS)


if __name__ == "__main__":
    main()
