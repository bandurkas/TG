"""Fetch Deribit ETH DVOL hourly history directly (works from Mac, unlike
Bybit). Paginates via the `continuation` cursor (1000 candles/call)."""
import requests
import json
import sys
import time

URL = "https://www.deribit.com/api/v2/public/get_volatility_index_data"


def fetch(currency: str, start_ms: int, end_ms: int, out_path: str):
    """API paginates BACKWARD: each call returns up to 1000 hourly bars
    ending at `end_timestamp`, plus a `continuation` cursor = the next
    (earlier) end_timestamp to request for older data."""
    rows = []
    cursor_end = end_ms
    while cursor_end > start_ms:
        params = {
            "currency": currency,
            "start_timestamp": start_ms,
            "end_timestamp": cursor_end,
            "resolution": 3600,
        }
        resp = requests.get(URL, params=params, timeout=15)
        resp.raise_for_status()
        result = resp.json()["result"]
        data = result["data"]
        if not data:
            break
        rows.extend(data)
        continuation = result.get("continuation")
        if continuation is None or continuation >= cursor_end:
            break
        cursor_end = continuation
        time.sleep(0.1)

    seen = set()
    dedup = []
    for r in sorted(rows, key=lambda r: r[0]):
        if r[0] in seen:
            continue
        seen.add(r[0])
        dedup.append(r)

    with open(out_path, "w") as f:
        json.dump(dedup, f)
    print(f"{out_path}: {len(dedup)} hourly bars")


if __name__ == "__main__":
    currency = sys.argv[1] if len(sys.argv) > 1 else "ETH"
    start_ms = int(sys.argv[2]) if len(sys.argv) > 2 else 1655515800000
    end_ms = int(sys.argv[3]) if len(sys.argv) > 3 else 1782414900000
    out_path = sys.argv[4] if len(sys.argv) > 4 else f"../data/{currency.lower()}_dvol_1h_4y.json"
    fetch(currency, start_ms, end_ms, out_path)
