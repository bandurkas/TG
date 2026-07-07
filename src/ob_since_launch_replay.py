from __future__ import annotations
"""'What if we'd used the new OB params from day 1' replay over Tyagach's
REAL launch-to-now calendar window (2026-06-25 21:59 UTC -> latest fetched
data), starting at the bot's real $2000.

Per user's explicit instruction: real BB/MB trades are kept EXACTLY as they
actually happened (fixed dollar pnl_net at their real exit timestamps, not
re-simulated) -- only OB is re-simulated, using the new per-TF depth/
r_target/expiry from finding_tyagach_ob_depth_sweep. The simulated OB
candidates compete for the SAME capacity/same-direction-conflict slots that
the real BB/MB positions actually occupied (per-TF sub-book rule, matching
live behavior per tyagach_samedir_ab.py / project_tyagach_paper_bot's 07-05
finding) -- real trades always execute unconditionally (they're historical
fact); only the hypothetical OB candidates can get blocked by them.

Needs FRESH data (fetched via VPS3 relay + direct Deribit call this session,
since the checked-in eth_*.csv files only covered through 2026-06-27,
before most of the real trade history)."""
import sys
import numpy as np

sys.path.insert(0, "/Users/sabar/Desktop/smc_options/src")
import structure
from dvol import load_dvol_aligned
import bs_pricer as bsp
from portfolio import PRIORITY, stats
from ob_depth_sweep import detect_ob_zones, find_depth_entry, MAX_LOOKAHEAD, BPD, DAYS_PER_YEAR, IV_THRESHOLD

DATA_DIR = "/Users/sabar/Desktop/smc_options/data"
DVOL_JSON = f"{DATA_DIR}/eth_dvol_1h_4y_fresh.json"
TF_FILES = {
    "15m": f"{DATA_DIR}/eth_15m_fresh.csv",
    "30m": f"{DATA_DIR}/eth_30m_fresh.csv",
    "1h": f"{DATA_DIR}/eth_1h_fresh.csv",
    "2h": f"{DATA_DIR}/eth_2h_fresh.csv",
}

STARTING_BALANCE = 2000.0
LAUNCH_TS = 1782424743912   # bot_state.started_at_ms, real launch
NOW_TS = 1783406700000      # latest fetched candle (2026-07-07 06:45 UTC)

# Real historical ACTIVE_CELLS windows per TF for OB specifically (reconstructed
# from `git log`/`git show` on services/config.py -- OB was NOT live on all 4 TFs
# the whole time: 1h/OB only exists since the 07-07 MB-deactivation deploy, and
# 15m/OB had a ~4-day gap between the 07-02 multi-TF launch and the same-day
# 07-03 review-correction commit that dropped it, until it came back 07-07).
# Using commit UTC timestamps as a deploy-time proxy (this session's commits are
# typically deployed within the hour of committing).
OB_ACTIVE_WINDOWS = {
    "15m": [(LAUNCH_TS, 1783014607000), (1783368119000, NOW_TS)],  # off 07-02 17:50 -> 07-06 20:02 UTC
    "30m": [(1782934382000, NOW_TS)],                               # born 07-01 19:33 UTC, on since
    "1h":  [(1783368119000, NOW_TS)],                               # born 07-06 20:02 UTC only
    "2h":  [(1782934382000, NOW_TS)],                               # born 07-01 19:33 UTC, on since
}


def in_active_window(tf: str, ts: int) -> bool:
    return any(a <= ts <= b for a, b in OB_ACTIVE_WINDOWS[tf])


IV_THRESHOLD_CHANGE_TS = 1783012433000  # commit 205ac32, 2026-07-03 00:13:53+0700: OB IV 60->50


def iv_threshold_at(ts: int) -> float:
    return 50.0 if ts >= IV_THRESHOLD_CHANGE_TS else 60.0
WEIGHT_PCT = {"OB": 0.12, "MB": 0.18, "BB": 0.28}
MAX_OPEN_PER_ZONE = {"OB": 3, "MB": 2, "BB": 1}
MAX_OPEN_TOTAL_GLOBAL = 8
MAX_TOTAL_MARGIN_PCT = 0.60
LOT_SIZE = 0.10
MARGIN_PCT = 0.15
FEE_RATE = 0.0003
FEE_CAP_PCT = 0.125

CANDIDATE_CFG = {
    "15m": dict(depth_frac=0.575, r_target=10.0, expiry_days=0.25),
    "30m": dict(depth_frac=0.500, r_target=7.0, expiry_days=0.25),
    "1h":  dict(depth_frac=0.325, r_target=8.0, expiry_days=0.75),
    "2h":  dict(depth_frac=0.675, r_target=3.0, expiry_days=1.00),
}
BASELINE_CFG = {tf: dict(depth_frac=0.50, r_target=3.0, expiry_days=0.5) for tf in TF_FILES}

# REAL BB/MB closed trades exactly as recorded in Tyagach's live sqlite DB
# (positions table, kept as-is per user instruction -- NOT re-simulated).
REAL_BB_MB = [
    # (tf, zone_kind, direction, entry_ts_ms, exit_ts_ms, pnl_net)
    ("15m", "BB", "bullish", 1782488700000, 1782500431920, -2.369),
    ("2h",  "MB", "bullish", 1782993600000, 1783012828378, -1.102),
    ("15m", "MB", "bullish", 1783058400000, 1783069258794,  0.099),
    ("15m", "MB", "bullish", 1783064700000, 1783071002439,  4.294),
    ("15m", "MB", "bullish", 1783081800000, 1783086324490,  5.012),
    ("15m", "MB", "bullish", 1783087200000, 1783089030328, -6.038),
    ("1h",  "MB", "bullish", 1783116000000, 1783123243103,  0.056),
    ("15m", "MB", "bullish", 1783142100000, 1783143951280,  0.319),
    ("1h",  "MB", "bullish", 1783173600000, 1783179009314,  6.934),
    ("15m", "MB", "bullish", 1783178100000, 1783214225021, -9.575),
    ("1h",  "MB", "bullish", 1783209600000, 1783217595951, -11.516),
    ("2h",  "MB", "bullish", 1783209600000, 1783217597423, -6.841),
    ("15m", "MB", "bullish", 1783273500000, 1783285607305, -3.464),
    ("2h",  "MB", "bullish", 1783317600000, 1783326242575, -3.462),
    ("15m", "MB", "bearish", 1783341000000, 1783341981768, -0.969),
    ("15m", "MB", "bullish", 1783341900000, 1783343005259, -7.465),
]
assert abs(sum(r[5] for r in REAL_BB_MB) - (-2.369 - 33.719)) < 0.01  # rounding noise from the 3dp SQL dump


def simulate_ob_entries(tf: str, cfg: dict, launch_ts: int):
    path = TF_FILES[tf]
    bpd = BPD[tf]
    max_lookahead = MAX_LOOKAHEAD[tf]
    df = structure.load_csv(path)
    n = len(df)
    ts = df["ts_ms"].values
    zones = detect_ob_zones(df)
    iv_series = load_dvol_aligned(DVOL_JSON, df)
    o, h, l, c = df["open"].values, df["high"].values, df["low"].values, df["close"].values
    depth_frac, rt, exp_days = cfg["depth_frac"], cfg["r_target"], cfg["expiry_days"]

    out = []
    for zone in zones:
        found = find_depth_entry(o, h, l, c, zone, n, depth_frac, max_lookahead)
        if found is None:
            continue
        entry_idx, entry_price, stop_price = found
        entry_ts = int(ts[entry_idx])
        if not in_active_window(tf, entry_ts):
            continue
        if abs(entry_price - stop_price) <= 0:
            continue
        iv0 = iv_series[entry_idx]
        if np.isnan(iv0) or iv0 * 100 <= iv_threshold_at(entry_ts):
            continue

        is_long = zone.direction == "bullish"
        risk = abs(entry_price - stop_price)
        tp_price = entry_price + rt * risk if is_long else entry_price - rt * risk
        expiry_bars = int(exp_days * bpd)
        expiry_idx = min(n - 1, entry_idx + expiry_bars)
        exit_idx = expiry_idx
        for j in range(entry_idx + 1, expiry_idx + 1):
            hit_sl = (l[j] <= stop_price) if is_long else (h[j] >= stop_price)
            hit_tp = (h[j] >= tp_price) if is_long else (l[j] <= tp_price)
            if hit_sl or hit_tp:
                exit_idx = j
                break
        spot_exit = c[exit_idx]
        side = "P" if is_long else "C"
        elapsed_days = (exit_idx - entry_idx) / bpd
        T_remaining = max(0.0, (exp_days - elapsed_days) / DAYS_PER_YEAR)
        T_entry = exp_days / DAYS_PER_YEAR
        strike = entry_price
        premium = bsp.price(side, entry_price, strike, T_entry, iv0)
        value_exit = bsp.price(side, spot_exit, strike, T_remaining, iv0)
        pnl_per_unit = premium - value_exit
        exit_ts = int(ts[exit_idx])
        out.append(("sim", tf, "OB", zone.direction, entry_ts, exit_ts, entry_price, pnl_per_unit))
    return out


def simulate_mixed(sim_ob_events, launch_ts: int):
    real_events = [("real", tf, kind, direction, entry_ts, exit_ts, None, pnl_net)
                   for tf, kind, direction, entry_ts, exit_ts, pnl_net in REAL_BB_MB
                   if entry_ts >= launch_ts]
    all_events = sorted(sim_ob_events + real_events,
                         key=lambda e: (e[4], PRIORITY.get(e[2], 9)))

    balance = STARTING_BALANCE
    open_positions = []  # (tf, kind, direction, exit_ts, is_real, num_units, ppu_or_none, notional, pnl_net_or_none)
    equity_curve = [(launch_ts, balance)]
    closed = []

    def fee(notional, premium_total):
        return min(notional * FEE_RATE, abs(premium_total) * FEE_CAP_PCT)

    def close_due(up_to_ts):
        nonlocal balance
        still = []
        for p in sorted(open_positions, key=lambda p: p[3]):
            if p[3] <= up_to_ts:
                tf, kind, direction, exit_ts, is_real, num_units, ppu, notional, pnl_net = p
                if is_real:
                    net = pnl_net
                else:
                    gross = ppu * num_units
                    premium_total = abs(ppu) * num_units
                    net = gross - 2 * fee(notional, premium_total)
                balance += net
                closed.append((tf, kind, exit_ts, net))
                equity_curve.append((exit_ts, balance))
            else:
                still.append(p)
        open_positions[:] = still

    for kind_tag, tf, kind, direction, entry_ts, exit_ts, spot_entry, payload in all_events:
        close_due(entry_ts)

        if kind_tag == "real":
            # historical fact -- always executes, unconditionally occupies its slot
            open_positions.append((tf, kind, direction, exit_ts, True, None, None, None, payload))
            continue

        # simulated OB candidate -- subject to the same live capacity rules
        conflict = any(p[0] == tf and p[2] == direction for p in open_positions)
        if conflict:
            continue
        per_zone = sum(1 for p in open_positions if p[0] == tf and p[1] == kind)
        if per_zone >= MAX_OPEN_PER_ZONE.get(kind, 0):
            continue
        if len(open_positions) >= MAX_OPEN_TOTAL_GLOBAL:
            continue
        if balance <= 0:
            continue
        budget = WEIGHT_PCT.get(kind, 0.0) * balance
        margin_per_lot = LOT_SIZE * spot_entry * MARGIN_PCT
        n_lots = int(budget // margin_per_lot) if margin_per_lot > 0 else 0
        if n_lots < 1:
            continue
        num_units = n_lots * LOT_SIZE
        notional = num_units * spot_entry
        margin_required = n_lots * margin_per_lot
        total_margin = sum((p[7] or 0) * MARGIN_PCT for p in open_positions if not p[4])
        if total_margin + margin_required > balance * MAX_TOTAL_MARGIN_PCT:
            continue
        open_positions.append((tf, kind, direction, exit_ts, False, num_units, payload, notional, None))

    if all_events:
        close_due(max(e[5] for e in all_events) + 1)
    return balance, equity_curve, closed


def run(cfg_by_tf: dict, label: str):
    sim_ob = []
    for tf, cfg in cfg_by_tf.items():
        events = simulate_ob_entries(tf, cfg, LAUNCH_TS)
        sim_ob.extend(events)
        print(f"[{label}] {tf}: {len(events)} simulated OB entries since launch", flush=True)
    balance, curve, closed = simulate_mixed(sim_ob, LAUNCH_TS)
    s = stats(STARTING_BALANCE, balance, curve, len(closed))
    n_ob = sum(1 for c in closed if c[1] == "OB")
    n_bbmb = sum(1 for c in closed if c[1] in ("BB", "MB"))
    ob_pnl = sum(c[3] for c in closed if c[1] == "OB")
    bbmb_pnl = sum(c[3] for c in closed if c[1] in ("BB", "MB"))
    print(f"\n=== {label}: final=${s['final_balance']} return={s['total_return_pct']:+.2f}% "
          f"maxDD={s['max_dd_pct']:.2f}% n_closed={s['n_closed']} "
          f"(OB:{n_ob} pnl=${ob_pnl:.2f} | BB/MB:{n_bbmb} pnl=${bbmb_pnl:.2f}) ===\n")
    return s


def main():
    print(f"Real actual outcome (ground truth): start=$2000.00 -> $1976.07 "
          f"(OB +$12.15 [5 trades] + BB -$2.37 [1] + MB -$33.72 [15])\n")
    run(BASELINE_CFG, "COUNTERFACTUAL: live OB params (0.50/3.0/0.5) all along, BB/MB real")
    run(CANDIDATE_CFG, "COUNTERFACTUAL: NEW tuned OB params all along, BB/MB real")


if __name__ == "__main__":
    main()
