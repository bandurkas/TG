from __future__ import annotations
"""Fetch ETH USDT-perp klines from Bybit's public API, relayed through VPS3
via ssh/sshpass (per user instruction), and append them onto the checked-in
eth_*.csv files, which stop at 2026-06-27 -- 5+ weeks before today and,
critically, before most of the bot's real launch-to-now window
(2026-06-25 -> today) and specifically before the back half of the
2026-06-26->07-07 MB live-loss window this session's MB reactivation
decision needs to be checked against.

Requires TYAGACH_VPS3_HOST / TYAGACH_VPS3_PASS env vars -- never hardcode
VPS3 credentials in this file, the repo is public on GitHub.

Paginates Bybit's /v5/market/kline (max 1000 bars/request) backwards from
now until it reaches bars already covered by the existing CSV, then
appends only the new bars and re-sorts/de-dupes by ts_ms.
"""
import csv
import json
import subprocess
import time

DATA_DIR = "/Users/styserg/Desktop/Tyagach/data"
TF_INTERVAL = {"15m": "15", "30m": "30", "1h": "60", "2h": "120"}
TF_FILES = {
    "15m": f"{DATA_DIR}/eth_15m.csv",
    "30m": f"{DATA_DIR}/eth_30m.csv",
    "1h": f"{DATA_DIR}/eth_1h_rs.csv",
    "2h": f"{DATA_DIR}/eth_2h.csv",
}
import os

SYMBOL = "ETHUSDT"
BASE_URL = "https://api.bybit.com/v5/market/kline"
TF_BAR_MS = {"15m": 15 * 60_000, "30m": 30 * 60_000, "1h": 60 * 60_000, "2h": 120 * 60_000}

# Relayed through VPS3 via ssh/sshpass (per user instruction -- Bybit's public
# kline API, unlike Deribit's, isn't reliably reachable directly). Credentials
# read from env, never hardcoded -- this repo is public on GitHub.
VPS3_HOST = os.environ["TYAGACH_VPS3_HOST"]  # e.g. "root@1.2.3.4"
VPS3_PASS = os.environ["TYAGACH_VPS3_PASS"]


def fetch_page(interval, end_ms=None):
    """Relayed through VPS3 (per user instruction) via ssh+curl, same public
    Bybit endpoint the live bot itself uses to fetch klines."""
    params = f"category=linear&symbol={SYMBOL}&interval={interval}&limit=1000"
    if end_ms is not None:
        params += f"&end={end_ms}"
    url = f"{BASE_URL}?{params}"
    result = subprocess.run(
        ["sshpass", "-p", VPS3_PASS, "ssh", "-o", "StrictHostKeyChecking=no", VPS3_HOST,
         f"curl -s --max-time 15 '{url}'"],
        capture_output=True, text=True, timeout=30, check=True,
    )
    data = json.loads(result.stdout)
    if data.get("retCode") != 0:
        raise RuntimeError(f"Bybit error: {data}")
    # Bybit returns newest-first: [ts_ms, open, high, low, close, volume, turnover]
    return data["result"]["list"]


def fetch_since(interval, since_ms):
    all_rows = []
    end_ms = None
    while True:
        page = fetch_page(interval, end_ms)
        if not page:
            break
        all_rows.extend(page)
        oldest_ts = int(page[-1][0])
        if oldest_ts <= since_ms:
            break
        end_ms = oldest_ts - 1
        time.sleep(0.15)  # be polite to the public endpoint
    return all_rows


def main():
    import time as _time
    now_ms = int(_time.time() * 1000)

    for tf, path in TF_FILES.items():
        with open(path) as f:
            reader = csv.reader(f)
            header = next(reader)
            rows = list(reader)
        last_ts_ms = int(rows[-1][0])
        print(f"[{tf}] existing last bar: {last_ts_ms} ({len(rows)} rows) -- fetching new bars since then...")

        new_raw = fetch_since(TF_INTERVAL[tf], last_ts_ms)
        new_by_ts = {}
        for r in new_raw:
            ts = int(r[0])
            if ts > last_ts_ms:
                new_by_ts[ts] = [str(ts), r[1], r[2], r[3], r[4], r[5], r[6]]

        # Drop the still-forming bar (open_ts + bar_ms > now) -- same class of
        # bug the P0 kline-cache fix caught earlier this session: a partial
        # bar's OHLC understates true high/low and must never be persisted as
        # if it were closed.
        bar_ms = TF_BAR_MS[tf]
        dropped = [ts for ts in new_by_ts if ts + bar_ms > now_ms]
        for ts in dropped:
            del new_by_ts[ts]

        new_sorted = [new_by_ts[ts] for ts in sorted(new_by_ts)]
        print(f"[{tf}] fetched {len(new_sorted)} new CLOSED bars "
              f"({new_sorted[0][0] if new_sorted else '-'} .. {new_sorted[-1][0] if new_sorted else '-'}), "
              f"dropped {len(dropped)} still-forming")

        with open(path, "a", newline="") as f:
            writer = csv.writer(f)
            writer.writerows(new_sorted)
        print(f"[{tf}] appended -> {path} now has {len(rows) + len(new_sorted)} rows\n")


if __name__ == "__main__":
    main()
