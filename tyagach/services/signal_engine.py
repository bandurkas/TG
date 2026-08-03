"""Online adaptation of the research's batch zone-detection + midpoint-entry
logic (smc_options/src/{structure,ob,bb,mb,zones}.py + options_backtest.py's
`_find_midpoint_entry`). Detectors are pure functions over a DataFrame, so we
simply re-run them on a rolling window every tick — cheap at a few thousand
bars. Zone IDENTITY across ticks is timestamp-based (`zone_key`), not the
positional index the detectors use internally, since that index shifts every
tick as the rolling window moves.

Multi-TF: every public function now takes a `tf` label (e.g. "15m") and uses
config.TIMEFRAMES[tf] for lookahead / stale thresholds.  zone_key is prefixed
with the TF label to prevent cross-TF collisions."""
from __future__ import annotations

import sys
import os
import time as _time
from dataclasses import dataclass

import pandas as pd

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "core"))
import structure  # noqa: E402
import ob  # noqa: E402
import bb  # noqa: E402
import mb  # noqa: E402
import zones as zones_mod  # noqa: E402

from . import config
from db import repo


def klines_to_df(klines: list[dict]) -> pd.DataFrame:
    df = pd.DataFrame(klines)
    df["ts"] = pd.to_datetime(df["ts_ms"], unit="ms")
    return df[["ts_ms", "ts", "open", "high", "low", "close", "volume"]].reset_index(drop=True)


def detect_zones(df: pd.DataFrame) -> list[zones_mod.Zone]:
    swings = structure.detect_swings(df, order=config.SWING_ORDER)
    swings, events = structure.label_and_track(df, swings)
    fvgs = structure.detect_fvg(df)
    obs = ob.detect_ob(df, swings, fvgs)
    bbs = bb.detect_bb(df, obs, events)
    mbs = mb.detect_mb(df, events, obs)
    return zones_mod.build_zones(obs, bbs, mbs, fvgs)


def atr_series(highs, lows, closes, period: int = config.ATR_PERIOD):
    """Wilder-style true range, simple rolling mean(period). Causal — index i
    only uses bars up to and including i, matching the ATR used to validate
    ATR_BUFFER_MULT (src/sl_buffer_atr_sweep.py's atr_series)."""
    highs, lows, closes = pd.Series(highs), pd.Series(lows), pd.Series(closes)
    prev_close = closes.shift(1).fillna(closes.iloc[0])
    tr = pd.concat([highs - lows, (highs - prev_close).abs(), (lows - prev_close).abs()], axis=1).max(axis=1)
    return tr.rolling(period, min_periods=period).mean().values


def zone_key(tf: str, z: zones_mod.Zone, formed_ts_ms: int) -> str:
    return f"{tf}:{z.kind}:{z.direction}:{formed_ts_ms}:{z.zone_low:.6f}:{z.zone_high:.6f}"


def sync_new_zones(df: pd.DataFrame, tf: str) -> None:
    """Detect zones on the current window and upsert any not already known.
    Only upserts zones whose (tf, kind) pair is in config.ACTIVE_CELLS.
    Idempotent — INSERT OR IGNORE on zone_key."""
    detected = detect_zones(df)
    for z in detected:
        if (tf, z.kind) not in config.ACTIVE_CELLS:
            continue
        # zones.py's build_zones sets valid_from = formed_idx + 1 — a zone only
        # becomes tradeable the bar AFTER it confirms, never on the confirmation
        # bar itself. Skip zones whose next bar hasn't closed yet; they'll be
        # picked up once it has.
        if z.formed_idx + 1 >= len(df):
            continue
        formed_ts_ms = int(df["ts_ms"].iloc[z.formed_idx])
        valid_from_ts_ms = int(df["ts_ms"].iloc[z.formed_idx + 1])
        key = zone_key(tf, z, formed_ts_ms)
        repo.upsert_zone_signal(key, tf, z.kind, z.direction, formed_ts_ms,
                                 valid_from_ts_ms, z.zone_low, z.zone_high)


@dataclass
class TriggeredEntry:
    zone_key: str
    timeframe: str
    kind: str
    direction: str
    entry_ts_ms: int
    entry_price: float
    stop_price: float


def scan_pending_zones(df: pd.DataFrame, tf: str) -> list[TriggeredEntry]:
    """For every zone_signals row still 'pending' for this TF, scan closed bars
    from its valid_from forward looking for (a) invalidation, (b) a retracement
    to that cell's configured entry depth (config.cell_config's depth_frac —
    0.5 midpoint by default, per-cell override for OB since 2026-07-08), or
    (c) expiry of the lookahead window. Uses the TF-specific max_lookahead and
    stale_after from config.TIMEFRAMES.

    A (tf, kind) pair can be removed from config.ACTIVE_CELLS after a signal
    was already stored as 'pending' (e.g. a live config re-review). Expire
    those here too — sync_new_zones only blocks new rows from being created,
    it doesn't retroactively stop ones already pending from triggering."""
    tf_cfg = config.TIMEFRAMES[tf]
    pending = repo.get_pending_zone_signals(tf)
    if not pending or df.empty:
        return []

    still_active = []
    for row in pending:
        if (tf, row["kind"]) not in config.ACTIVE_CELLS:
            repo.set_zone_signal_status(row["zone_key"], "expired")
        else:
            still_active.append(row)
    pending = still_active
    if not pending:
        return []

    ts_to_idx = {int(ts): i for i, ts in enumerate(df["ts_ms"].values)}
    highs, lows, closes = df["high"].values, df["low"].values, df["close"].values
    n = len(df)
    triggered: list[TriggeredEntry] = []
    needs_atr = any(tf == cell_tf for cell_tf, _ in config.ATR_BUFFER_MULT)
    atr = atr_series(highs, lows, closes) if needs_atr else None

    for row in pending:
        start_idx = ts_to_idx.get(row["valid_from_ts_ms"])
        if start_idx is None:
            # zone's valid_from bar already fell out of the rolling window without
            # ever triggering — can't be evaluated anymore, drop it.
            repo.set_zone_signal_status(row["zone_key"], "expired")
            continue

        is_long = row["direction"] == "bullish"
        zlo, zhi = row["zone_low"], row["zone_high"]
        depth_frac = config.cell_config(tf, row["kind"])["depth_frac"]
        # 0.0 = touch the near edge, 0.5 = zone midpoint (the old hardcoded
        # value), 1.0 = touch the far edge. See config.CELL_CONFIG.
        entry_level = (zhi - depth_frac * (zhi - zlo)) if is_long else (zlo + depth_frac * (zhi - zlo))
        atr_mult = config.ATR_BUFFER_MULT.get((tf, row["kind"]))
        atr_val = atr[start_idx] if (atr_mult is not None and start_idx < len(atr)) else None
        if atr_mult is not None and atr_val is not None and not pd.isna(atr_val):
            buf = atr_mult * atr_val
        else:
            flat_frac = config.FLAT_BUFFER_FRAC_OVERRIDE.get((tf, row["kind"]), config.BUFFER_FRAC)
            buf = flat_frac * ((zlo + zhi) / 2)
        stop_price = (zlo - buf) if is_long else (zhi + buf)
        end_idx = min(n - 1, start_idx + tf_cfg.max_lookahead)

        resolved = False
        for i in range(start_idx, end_idx + 1):
            if is_long and closes[i] < stop_price:
                repo.set_zone_signal_status(row["zone_key"], "invalidated")
                resolved = True
                break
            if (not is_long) and closes[i] > stop_price:
                repo.set_zone_signal_status(row["zone_key"], "invalidated")
                resolved = True
                break
            touched = (lows[i] <= entry_level) if is_long else (highs[i] >= entry_level)
            if touched:
                # Rejection-close filter (2026-08-02, RESEARCH_FINDINGS_2026-08-02.md
                # U1): a wick through entry_level that doesn't also close back on
                # the favorable side is a fakeout, not a real touch -- keep scanning
                # instead of triggering on it. Validated across every (kind, tf)
                # tested this session; biggest single improvement found.
                rejected = (closes[i] > entry_level) if is_long else (closes[i] < entry_level)
                if not rejected:
                    continue
                touch_ts_ms = int(df["ts_ms"].iloc[i])
                if (n - 1 - i) > tf_cfg.stale_after:
                    # Too old to act on with today's live quote — e.g. a cold-start
                    # backlog. Expire rather than trade a historical touch against the
                    # wrong price context.
                    repo.set_zone_signal_status(row["zone_key"], "expired")
                elif _time.gmtime(touch_ts_ms / 1000).tm_hour in config.ENTRY_VETO_UTC_HOURS:
                    # Entry-hour veto (config.ENTRY_VETO_UTC_HOURS): consume the
                    # touch without trading — deferring it instead would turn the
                    # veto into an untested "delay to 16:00" variant.
                    repo.set_zone_signal_status(row["zone_key"], "expired")
                else:
                    triggered.append(TriggeredEntry(row["zone_key"], tf, row["kind"],
                                                     row["direction"], touch_ts_ms,
                                                     entry_level, stop_price))
                resolved = True
                break
        if not resolved and end_idx >= start_idx + tf_cfg.max_lookahead:
            repo.set_zone_signal_status(row["zone_key"], "expired")

    return triggered
