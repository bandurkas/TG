"""Tests for execution._paper_fill fee model.

Real Bybit fee = 0.03% of UNDERLYING notional (qty * spot price),
capped at 12.5% of the option premium per side.

Run: cd tyagach && python3 tests/test_fee_math.py
"""
from __future__ import annotations

import os
import sys
import types

# pybit lives inside the Docker container, not on the Mac host — stub it so
# execution.py can be imported and _paper_fill (which doesn't use the session)
# can be tested without a live Bybit connection.
_pybit = types.ModuleType("pybit")
_unified = types.ModuleType("pybit.unified_trading")


class _HTTP:
    def __init__(self, **kwargs): pass


_unified.HTTP = _HTTP
_pybit.unified_trading = _unified
sys.modules.setdefault("pybit", _pybit)
sys.modules.setdefault("pybit.unified_trading", _unified)

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from services import execution


class _MockSession:
    pass


def _client() -> execution.ExecutionClient:
    return execution.ExecutionClient(session=_MockSession())


def test_fee_uses_underlying_notional():
    # 0.1 ETH, premium = 50 USDT/unit, spot = 2000 USDT
    # underlying_notional = 0.1 * 2000 = 200
    # fee = min(200 * 0.0003, 5 * 0.125) = min(0.06, 0.625) = 0.06
    result = _client()._paper_fill("ETH-X-C-USDT", qty=0.1, limit_price=50.0, entry_spot=2000.0)
    assert abs(result.fees - 0.06) < 1e-9


def test_fee_cap_applies_when_premium_tiny():
    # premium very small → cap wins
    # 0.1 ETH, premium = 0.01/unit, spot = 2000
    # underlying = 200; uncapped = 0.06; cap = 0.001 * 0.125 = 0.000125
    result = _client()._paper_fill("ETH-X-C-USDT", qty=0.1, limit_price=0.01, entry_spot=2000.0)
    expected = min(0.1 * 2000.0 * 0.0003, 0.1 * 0.01 * 0.125)
    assert abs(result.fees - expected) < 1e-12


def test_fee_fallback_when_no_spot():
    # No entry_spot (0.0): fallback to premium_notional for underlying
    # premium_notional = 0.1 * 50 = 5; fee = min(5*0.0003, 5*0.125) = 0.0015
    result = _client()._paper_fill("ETH-X-C-USDT", qty=0.1, limit_price=50.0, entry_spot=0.0)
    assert abs(result.fees - 0.0015) < 1e-9


def test_paper_fill_status_and_qty():
    result = _client()._paper_fill("ETH-X-C-USDT", qty=0.2, limit_price=30.0, entry_spot=1800.0)
    assert result.is_filled
    assert result.avg_price == 30.0
    assert result.filled_qty == 0.2
    assert result.status == "Filled"


def test_fee_significantly_higher_with_correct_spot_vs_fallback():
    # With spot = 2000, fee = 0.03% of 200 = 0.06
    # Without spot (fallback), fee = 0.03% of 5 = 0.0015
    # ~40x difference — validates the fix matters
    with_spot = _client()._paper_fill("ETH-X-C-USDT", qty=0.1, limit_price=50.0, entry_spot=2000.0)
    without_spot = _client()._paper_fill("ETH-X-C-USDT", qty=0.1, limit_price=50.0, entry_spot=0.0)
    assert with_spot.fees / without_spot.fees > 30


if __name__ == "__main__":
    tests = [v for k, v in list(globals().items()) if k.startswith("test_")]
    for t in tests:
        t()
        print(f"OK: {t.__name__}")
    print(f"\n{len(tests)} tests passed")
