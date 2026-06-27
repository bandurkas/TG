"""Fire-and-forget Telegram notifications for the Tyagach loop.

Mirrors opt-app's services/telegram_notify.py (same bot-tagging convention —
Tyagach posts to the SAME chat as Sniper1/Boba1/Grogu1, so every message is
tagged "[Tyagach]"), but standalone since this is a separate repo/deploy.

Stateless, no separate process. If TELEGRAM_BOT_TOKEN or TELEGRAM_CHAT_ID env
vars are missing, every notify() call is a no-op so the loop never breaks
because of telemetry config.
"""
from __future__ import annotations

import os
from typing import Final

import requests

_TOKEN: Final = os.environ.get("TELEGRAM_BOT_TOKEN", "").strip()
# Comma/space-separated list, same convention as opt-app.
_CHAT_IDS: Final = [c for c in os.environ.get("TELEGRAM_CHAT_ID", "").replace(",", " ").split() if c]
_TIMEOUT_S: Final = 5

BOT_LABEL: Final = "Tyagach"


def is_enabled() -> bool:
    return bool(_TOKEN and _CHAT_IDS)


def notify(text: str, *, parse_mode: str = "HTML", silent: bool = False) -> None:
    """Send a message to every configured chat. Never raises — the loop should never break on this."""
    if not is_enabled():
        return
    text = f"<b>[{BOT_LABEL}]</b> {text}"
    for chat_id in _CHAT_IDS:
        try:
            requests.post(
                f"https://api.telegram.org/bot{_TOKEN}/sendMessage",
                json={
                    "chat_id": chat_id,
                    "text": text,
                    "parse_mode": parse_mode,
                    "disable_notification": silent,
                },
                timeout=_TIMEOUT_S,
            )
        except Exception as e:  # noqa: BLE001
            print(f"[telegram] notify failed for {chat_id}: {e!r}", flush=True)


def notify_open(*, zone_kind: str, option_side: str, symbol: str, strike: float,
                qty: float, premium_recv: float, fee: float, balance_now: float) -> None:
    side_word = "CALL" if option_side == "C" else "PUT"
    text = (
        f"🟢 <b>OPENED</b> · {zone_kind} SELL {side_word}\n"
        f"  Strike: ${strike:.0f} · qty {qty:.4f}\n"
        f"  Premium received: <b>${premium_recv:.2f}</b> · fee ${fee:.2f}\n"
        f"  Balance now: <b>${balance_now:.2f}</b>\n"
        f"  Symbol: <code>{symbol}</code>"
    )
    notify(text)


def notify_close(*, symbol: str, reason: str, pnl_net: float, balance_after: float,
                 total_pnl_usd: float) -> None:
    profit = pnl_net > 0
    emoji = "✅" if profit else "❌"
    sign = "+" if profit else "-"
    total_sign = "+" if total_pnl_usd >= 0 else "-"
    text = (
        f"{emoji} <b>CLOSED</b> <code>{symbol}</code>\n"
        f"  Reason: {reason.upper()}\n"
        f"  This trade: <b>{sign}${abs(pnl_net):.2f}</b>\n"
        f"  Balance now: <b>${balance_after:.2f}</b>\n"
        f"  Total P&amp;L since start: <b>{total_sign}${abs(total_pnl_usd):.2f}</b>"
    )
    notify(text)


def notify_skipped(*, reason: str, detail: str) -> None:
    text = f"⚠️ <b>Signal skipped</b> — {reason}\n  {detail}"
    notify(text, silent=True)
