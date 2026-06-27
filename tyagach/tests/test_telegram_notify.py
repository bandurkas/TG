"""Unit tests for Tyagach's telegram_notify.py (2026-06-27, new module —
Tyagach previously had zero Telegram notifications, console logs only).

Monkeypatches requests.post and is_enabled() so tests run without
TELEGRAM_BOT_TOKEN/CHAT_ID configured.

Run: cd tyagach && python3 tests/test_telegram_notify.py
"""
from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from services import telegram_notify as tn


def _capture():
    sent = []
    orig_enabled = tn.is_enabled
    orig_post = tn.requests.post
    tn.is_enabled = lambda: True
    tn._CHAT_IDS[:] = ["fake_chat"]

    class _Resp:
        status_code = 200

    def fake_post(url, json, timeout):
        sent.append(json["text"])
        return _Resp()

    tn.requests.post = fake_post
    return sent, orig_enabled, orig_post


def _restore(orig_enabled, orig_post):
    tn.is_enabled = orig_enabled
    tn.requests.post = orig_post


def test_every_message_tagged_tyagach():
    sent, oe, op = _capture()
    try:
        tn.notify("hello")
    finally:
        _restore(oe, op)
    assert sent == ["<b>[Tyagach]</b> hello"]


def test_notify_open_format():
    sent, oe, op = _capture()
    try:
        tn.notify_open(zone_kind="BB", option_side="P", symbol="ETH-3JUL26-1575-P-USDT",
                       strike=1575.0, qty=2.3, premium_recv=114.08, fee=0.34, balance_now=2004.02)
    finally:
        _restore(oe, op)
    text = sent[0]
    assert "[Tyagach]" in text
    assert "OPENED" in text and "BB SELL PUT" in text
    assert "Premium received: <b>$114.08</b>" in text
    assert "Balance now: <b>$2004.02</b>" in text


def test_notify_close_format_loss():
    sent, oe, op = _capture()
    try:
        tn.notify_close(symbol="ETH-3JUL26-1575-P-USDT", reason="sl", pnl_net=-2.37,
                        balance_after=2004.02, total_pnl_usd=4.02)
    finally:
        _restore(oe, op)
    text = sent[0]
    assert "This trade: <b>-$2.37</b>" in text
    assert "Balance now: <b>$2004.02</b>" in text
    assert "Total P&amp;L since start: <b>+$4.02</b>" in text


def test_disabled_sends_nothing():
    sent = []
    orig_enabled = tn.is_enabled
    orig_post = tn.requests.post
    tn.is_enabled = lambda: False
    tn.requests.post = lambda *a, **k: sent.append(1)
    try:
        tn.notify("should not send")
    finally:
        tn.is_enabled = orig_enabled
        tn.requests.post = orig_post
    assert sent == []


if __name__ == "__main__":
    tests = [v for k, v in list(globals().items()) if k.startswith("test_")]
    for t in tests:
        t()
        print(f"OK: {t.__name__}")
    print(f"\n{len(tests)} tests passed")
