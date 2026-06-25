"""Online adaptation of the research's batch zone-detection + midpoint-entry
logic (smc_options/src/{structure,ob,bb,mb,zones}.py + options_backtest.py's
`_find_midpoint_entry`). Detectors are pure functions over a DataFrame, so we
simply re-run them on a rolling window every tick — cheap at a few thousand
15m bars. Zone IDENTITY across ticks is timestamp-based (`zone_key`), not the
positional index the detectors use internally, since that index shifts every
tick as the rolling window moves."""
from __future__ import annotations

import sys
import os
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
    return zones_mod.build_zones(obs, bbs, mbs)


def zone_key(z: zones_mod.Zone, formed_ts_ms: int) -> str:
    return f"{z.kind}:{z.direction}:{formed_ts_ms}:{z.zone_low:.6f}:{z.zone_high:.6f}"


def sync_new_zones(df: pd.DataFrame) -> None:
    """Detect zones on the current window and upsert any not already known.
    Idempotent — INSERT OR IGNORE on zone_key."""
    detected = detect_zones(df)
    for z in detected:
        # zones.py's build_zones sets valid_from = formed_idx + 1 — a zone only
        # becomes tradeable the bar AFTER it confirms, never on the confirmation
        # bar itself. Skip zones whose next bar hasn't closed yet; they'll be
        # picked up once it has.
        if z.formed_idx + 1 >= len(df):
            continue
        formed_ts_ms = int(df["ts_ms"].iloc[z.formed_idx])
        valid_from_ts_ms = int(df["ts_ms"].iloc[z.formed_idx + 1])
        key = zone_key(z, formed_ts_ms)
        repo.upsert_zone_signal(key, z.kind, z.direction, formed_ts_ms, valid_from_ts_ms,
                                 z.zone_low, z.zone_high)


@dataclass
class TriggeredEntry:
    zone_key: str
    kind: str
    direction: str
    entry_ts_ms: int
    entry_price: float
    stop_price: float


def scan_pending_zones(df: pd.DataFrame) -> list[TriggeredEntry]:
    """For every zone_signals row still 'pending', scan closed bars from its
    valid_from forward looking for (a) invalidation — close beyond the stop
    buffer before any touch, (b) a midpoint touch — the trigger, or (c)
    expiry of the lookahead window with neither. Mutates zone_signals status
    in the DB (invalidated/expired) and returns newly triggered entries
    (status is left 'pending' for those — the caller marks them 'triggered'
    only after successfully acting on them, so a crash mid-tick doesn't lose
    a signal)."""
    pending = repo.get_pending_zone_signals()
    if not pending or df.empty:
        return []

    ts_to_idx = {int(ts): i for i, ts in enumerate(df["ts_ms"].values)}
    highs, lows, closes = df["high"].values, df["low"].values, df["close"].values
    n = len(df)
    triggered: list[TriggeredEntry] = []

    for row in pending:
        start_idx = ts_to_idx.get(row["valid_from_ts_ms"])
        if start_idx is None:
            # zone's valid_from bar already fell out of the rolling window without
            # ever triggering — can't be evaluated anymore, drop it.
            repo.set_zone_signal_status(row["zone_key"], "expired")
            continue

        is_long = row["direction"] == "bullish"
        zlo, zhi = row["zone_low"], row["zone_high"]
        mid = (zlo + zhi) / 2
        buf = config.BUFFER_FRAC * mid
        stop_price = (zlo - buf) if is_long else (zhi + buf)
        end_idx = min(n - 1, start_idx + config.MAX_ZONE_LOOKAHEAD)

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
            touched = (lows[i] <= mid) if is_long else (highs[i] >= mid)
            if touched:
                if (n - 1 - i) > config.STALE_AFTER_BARS:
                    # Too old to act on with today's quote (see config.STALE_AFTER_BARS) —
                    # e.g. a multi-day cold-start backlog or outage gap. Expire it rather
                    # than trading a historical touch against a live price that has nothing
                    # to do with the signal's actual context.
                    repo.set_zone_signal_status(row["zone_key"], "expired")
                else:
                    triggered.append(TriggeredEntry(row["zone_key"], row["kind"], row["direction"],
                                                     int(df["ts_ms"].iloc[i]), mid, stop_price))
                resolved = True
                break
        if not resolved and end_idx >= start_idx + config.MAX_ZONE_LOOKAHEAD:
            repo.set_zone_signal_status(row["zone_key"], "expired")

    return triggered
