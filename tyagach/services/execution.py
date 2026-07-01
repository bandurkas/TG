"""Authenticated Bybit options execution client — Tyagach's own copy,
mirroring opt-app's services/execution.py conventions (never assume a fill;
read back avgPrice/cumExecQty from the exchange) but trimmed to this
project's single-account, single-coin (ETH) scope.

The account behind BYBIT_API_KEY/SECRET is a REAL Bybit mainnet account
(not Bybit's separate testnet environment — that was the original plan,
superseded 2026-06-26 once the user supplied a real-account key instead).
Gated by config.TRADING_MODE ("paper" by default): real-account calls for
market data (instruments/quotes/wallet/account info) always run, but
sell_to_open/buy_to_close only place a REAL order when
config.is_live() — otherwise they simulate a fill against the real live
quote (see _paper_fill). Flip TYAGACH_TRADING_MODE=live in .env when ready
to arm real execution; nothing else needs to change."""
from __future__ import annotations

import os
import time
import uuid
from typing import NamedTuple

from pybit.unified_trading import HTTP

from . import config

_TERMINAL = {"Filled", "PartiallyFilled", "Cancelled", "Rejected", "Deactivated"}


class OrderResult(NamedTuple):
    order_id: str
    avg_price: float
    filled_qty: float
    fees: float
    status: str

    @property
    def is_filled(self) -> bool:
        return self.filled_qty > 0 and self.status in ("Filled", "PartiallyFilled")


class ExecutionError(Exception):
    pass


def _use_testnet() -> bool:
    # Default false: BYBIT_API_KEY/SECRET is a real mainnet account (see
    # module docstring) — only set BYBIT_TESTNET=true if a genuine
    # testnet.bybit.com key is ever swapped in instead.
    return os.environ.get("BYBIT_TESTNET", "false").strip().lower() in ("1", "true", "yes")


def _credentials() -> tuple[str | None, str | None]:
    return os.environ.get("BYBIT_API_KEY"), os.environ.get("BYBIT_API_SECRET")


class ExecutionClient:
    CATEGORY = "option"

    def __init__(self, session=None):
        if session is not None:
            self.session = session
            return
        key, secret = _credentials()
        if not key or not secret:
            raise ExecutionError("missing BYBIT_API_KEY/BYBIT_API_SECRET in environment")
        self.session = HTTP(testnet=_use_testnet(), api_key=key, api_secret=secret)

    def account_info(self) -> dict:
        out: dict = {}
        try:
            info = self.session.get_api_key_information()
            res = (info or {}).get("result", {})
            out["uta"] = res.get("uta")
            out["permissions"] = res.get("permissions", {})
            out["readOnly"] = res.get("readOnly")
        except Exception as e:  # noqa: BLE001
            out["error"] = repr(e)
        return out

    def wallet_usdt(self) -> float | None:
        try:
            resp = self.session.get_wallet_balance(accountType="UNIFIED", coin="USDT")
            lst = (resp or {}).get("result", {}).get("list", [])
            if not lst:
                return None
            coins = lst[0].get("coin", [])
            for c in coins:
                if c.get("coin") == "USDT":
                    return float(c.get("walletBalance") or 0.0)
            return None
        except Exception as e:  # noqa: BLE001
            print(f"[execution] wallet_usdt error: {e}", flush=True)
            return None

    def find_instrument(self, side: str, target_strike: float, target_expiry_days: float) -> dict | None:
        """Picks the live ETH option instrument nearest to the target strike
        and expiry, among instruments expiring AT OR AFTER target_expiry_days
        (never pick an expiry shorter than what the strategy needs)."""
        try:
            resp = self.session.get_instruments_info(category=self.CATEGORY, baseCoin=config.BASE_COIN)
            items = (resp or {}).get("result", {}).get("list", [])
        except Exception as e:  # noqa: BLE001
            print(f"[execution] find_instrument error: {e}", flush=True)
            return None

        now_ms = int(time.time() * 1000)
        target_expiry_ms = now_ms + int(target_expiry_days * 86400 * 1000)
        candidates = []
        for it in items:
            if it.get("optionsType") != ("Call" if side == "C" else "Put"):
                continue
            try:
                delivery_ms = int(it.get("deliveryTime", 0))
                strike = float(it.get("symbol", "").split("-")[2])
            except (ValueError, IndexError):
                continue
            if delivery_ms < target_expiry_ms:
                continue
            candidates.append((delivery_ms, abs(strike - target_strike), it))

        if not candidates:
            return None
        candidates.sort(key=lambda t: (t[0], t[1]))  # nearest valid expiry first, then nearest strike
        return candidates[0][2]

    def get_quote(self, symbol: str) -> dict | None:
        try:
            resp = self.session.get_tickers(category=self.CATEGORY, symbol=symbol)
            row = resp["result"]["list"][0]
            return {"bid": float(row.get("bid1Price") or 0.0), "ask": float(row.get("ask1Price") or 0.0),
                    "mark": float(row.get("markPrice") or 0.0)}
        except Exception as e:  # noqa: BLE001
            print(f"[execution] get_quote error ({symbol}): {e}", flush=True)
            return None

    def round_qty(self, instrument: dict, qty: float) -> float:
        step = float(instrument.get("lotSizeFilter", {}).get("qtyStep", config.LOT_SIZE))
        return round(round(qty / step) * step, 6)

    def sell_to_open(self, symbol: str, qty: float, limit_price: float,
                      entry_spot: float = 0.0) -> OrderResult | None:
        if not config.is_live():
            return self._paper_fill(symbol, qty, limit_price, entry_spot)
        return self._place_and_confirm(symbol, "Sell", qty, limit_price)

    def buy_to_close(self, symbol: str, qty: float, limit_price: float,
                      entry_spot: float = 0.0) -> OrderResult | None:
        if not config.is_live():
            return self._paper_fill(symbol, qty, limit_price, entry_spot)
        return self._place_and_confirm(symbol, "Buy", qty, limit_price)

    def _paper_fill(self, symbol: str, qty: float, limit_price: float,
                     entry_spot: float = 0.0) -> OrderResult:
        """Simulated fill for paper mode — no place_order call, nothing
        touches the real account's positions/balance.

        Fee model: real Bybit options taker = 0.03% of UNDERLYING notional
        (qty * spot price), capped at 12.5% of the option premium per side.
        `entry_spot` is the ETH spot at entry/exit; if not supplied (legacy
        callers) we fall back to the old premium-based estimate which the
        caller comment explains is ~100x too low — always pass entry_spot."""
        premium_notional = qty * limit_price
        if entry_spot > 0:
            underlying_notional = qty * entry_spot
        else:
            underlying_notional = premium_notional  # fallback: same as old behaviour
        fee = min(underlying_notional * config.FEE_RATE, premium_notional * config.FEE_CAP_PCT)
        order_id = f"PAPER-{uuid.uuid4().hex[:20]}"
        return OrderResult(order_id, limit_price, qty, fee, "Filled")

    def _place_and_confirm(self, symbol: str, side: str, qty: float, limit_price: float) -> OrderResult | None:
        link_id = uuid.uuid4().hex[:24]
        try:
            resp = self.session.place_order(
                category=self.CATEGORY, symbol=symbol, side=side, orderType="Limit",
                qty=str(qty), price=str(round(limit_price, 2)), timeInForce="IOC",
                orderLinkId=link_id,
            )
            order_id = resp["result"]["orderId"]
        except Exception as e:  # noqa: BLE001
            print(f"[execution] place_order error ({symbol} {side} {qty}): {e}", flush=True)
            return None

        for _ in range(10):
            time.sleep(0.5)
            try:
                resp = self.session.get_order_history(category=self.CATEGORY, orderId=order_id)
                rows = resp["result"]["list"]
            except Exception as e:  # noqa: BLE001
                print(f"[execution] order status check error ({order_id}): {e}", flush=True)
                continue
            if not rows:
                continue
            row = rows[0]
            status = row.get("orderStatus")
            if status in _TERMINAL:
                filled_qty = float(row.get("cumExecQty") or 0.0)
                avg_price = float(row.get("avgPrice") or 0.0)
                fees = float(row.get("cumExecFee") or 0.0)
                return OrderResult(order_id, avg_price, filled_qty, fees, status)
        print(f"[execution] order {order_id} ({symbol}) never reached a terminal state in time", flush=True)
        return None


_client: ExecutionClient | None = None


def get_client() -> ExecutionClient:
    global _client
    if _client is None:
        _client = ExecutionClient()
    return _client
