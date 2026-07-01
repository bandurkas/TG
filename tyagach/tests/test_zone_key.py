"""Tests for signal_engine.zone_key — TF prefix prevents cross-TF key collisions.

Run: cd tyagach && python3 tests/test_zone_key.py
"""
from __future__ import annotations

import os
import sys
import types

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from services.signal_engine import zone_key


def _zone(kind="OB", direction="bullish", zone_low=1500.0, zone_high=1510.0):
    return types.SimpleNamespace(kind=kind, direction=direction,
                                  zone_low=zone_low, zone_high=zone_high)


def test_zone_key_starts_with_tf_prefix():
    assert zone_key("15m", _zone(), 1750000000000).startswith("15m:")
    assert zone_key("2h",  _zone(), 1750000000000).startswith("2h:")


def test_same_zone_different_tf_gives_different_keys():
    z = _zone()
    ts = 1750000000000
    assert zone_key("15m", z, ts) != zone_key("30m", z, ts)
    assert zone_key("15m", z, ts) != zone_key("1h",  z, ts)
    assert zone_key("15m", z, ts) != zone_key("2h",  z, ts)


def test_same_tf_same_zone_gives_same_key():
    z = _zone()
    ts = 1750000000000
    assert zone_key("15m", z, ts) == zone_key("15m", z, ts)


def test_zone_key_parts():
    z = _zone(kind="BB", direction="bearish", zone_low=1600.0, zone_high=1620.0)
    key = zone_key("2h", z, 1750000000000)
    parts = key.split(":")
    assert parts[0] == "2h"
    assert parts[1] == "BB"
    assert parts[2] == "bearish"
    assert parts[3] == "1750000000000"
    assert abs(float(parts[4]) - 1600.0) < 1e-4
    assert abs(float(parts[5]) - 1620.0) < 1e-4


def test_zone_key_different_timestamps_different_keys():
    z = _zone()
    assert zone_key("15m", z, 1750000000000) != zone_key("15m", z, 1750000900000)


def test_zone_key_no_tf_collision_across_all_active_tfs():
    z = _zone(kind="OB", direction="bullish", zone_low=1500.0, zone_high=1510.0)
    ts = 1750000000000
    keys = [zone_key(tf, z, ts) for tf in ("15m", "30m", "1h", "2h")]
    assert len(set(keys)) == 4  # all unique


if __name__ == "__main__":
    tests = [v for k, v in list(globals().items()) if k.startswith("test_")]
    for t in tests:
        t()
        print(f"OK: {t.__name__}")
    print(f"\n{len(tests)} tests passed")
