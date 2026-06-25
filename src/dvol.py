from __future__ import annotations
"""Load the real Deribit ETH DVOL hourly history and align it to the 15m
spot candle grid via forward-fill (DVOL updates hourly; each 15m candle gets
the most recent known DVOL print, no lookahead)."""
import json
import numpy as np
import pandas as pd


def load_dvol_aligned(dvol_json_path: str, spot_df: pd.DataFrame) -> np.ndarray:
    with open(dvol_json_path) as f:
        raw = json.load(f)
    dvol = pd.DataFrame(raw, columns=["ts_ms", "open", "high", "low", "close"])
    dvol = dvol.sort_values("ts_ms")
    merged = pd.merge_asof(
        spot_df[["ts_ms"]], dvol[["ts_ms", "close"]],
        on="ts_ms", direction="backward",
    )
    sigma = (merged["close"] / 100.0).values
    return sigma  # NaN where spot candle precedes all DVOL history
