"""Tests for the 2026-08-02 follow-up round: 15m/OB re-added (re-picked
config) and all 4 MB timeframes re-added (rejection-close reverses the
2026-07-07 live-loss-driven deactivation on backtest -- see config.py's
CELL_CONFIG comment for the full reasoning and caution).

Run: cd tyagach && python3 tests/test_mb_ob_reactivation.py
"""
from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from services import config


def test_mb_active_on_all_four_timeframes():
    for tf in ("15m", "30m", "1h", "2h"):
        assert (tf, "MB") in config.ACTIVE_CELLS, f"{tf}/MB should be active"


def test_15m_ob_reactivated():
    assert ("15m", "OB") in config.ACTIVE_CELLS


def test_mb_cell_configs_match_validated_values():
    # 15m/MB r_target 7.0->2.1 as of the 2026-08-03 TP retune
    # (tp_retarget_sweep.py) -- others unchanged by that round.
    expected = {
        "15m": {"depth_frac": 0.425, "r_target": 2.1, "expiry_days": 0.125},
        "30m": {"depth_frac": 0.400, "r_target": 10.0, "expiry_days": 0.167},
        "1h": {"depth_frac": 0.400, "r_target": 10.0, "expiry_days": 0.25},
        "2h": {"depth_frac": 0.500, "r_target": 3.0, "expiry_days": 0.25},
    }
    for tf, exp in expected.items():
        cfg = config.cell_config(tf, "MB")
        for key, val in exp.items():
            assert cfg[key] == val, f"{tf}/MB {key}: expected {val}, got {cfg[key]}"


def test_15m_ob_cell_config_matches_repicked_value():
    cfg = config.cell_config("15m", "OB")
    assert cfg["depth_frac"] == 0.700
    assert cfg["r_target"] == 4.0
    assert cfg["expiry_days"] == 0.083


def test_bb_deactivated_2026_08_03():
    # 15m/BB was deactivated 2026-08-03: catastrophic on full current data at
    # its live 5-day expiry_days (calmar -1.00, 8/8 negative quarters), and
    # even the best short-hold alternative previously tried stayed marginal
    # (holdout calmar -0.24) -- structural fragility, not a tuning miss.
    assert ("15m", "BB") not in config.CELL_CONFIG
    assert config.cell_config("15m", "BB") == config.ZONE_CONFIG["BB"]  # untouched default, just unused
    assert ("15m", "BB") not in config.ACTIVE_CELLS


def test_mb_already_had_sizing_tiers_before_this_round():
    # PRIORITY/WEIGHT_PCT/MAX_OPEN_PER_ZONE for MB were never removed when
    # MB was deactivated 2026-07-07 -- only ACTIVE_CELLS/CELL_CONFIG
    # changed. Confirms no KeyError risk reactivating it.
    assert "MB" in config.PRIORITY
    assert "MB" in config.WEIGHT_PCT
    assert "MB" in config.MAX_OPEN_PER_ZONE
    assert config.WEIGHT_PCT["MB"] > 0
    assert config.MAX_OPEN_PER_ZONE["MB"] > 0


if __name__ == "__main__":
    tests = [v for k, v in list(globals().items()) if k.startswith("test_")]
    for t in tests:
        t()
        print(f"OK: {t.__name__}")
    print(f"\n{len(tests)} tests passed")
