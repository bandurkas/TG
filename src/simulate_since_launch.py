from __future__ import annotations
"""What-if, restricted to the bot's ACTUAL live runtime window (not full
4y history): if today's U4 config (8 cells, addce8a) had been active since
the live bot's actual launch, how would it have done over that same
calendar span -- directly comparable to the bot's real realized result
(started_at_ms=1782424743912 i.e. 2026-06-25 21:59 UTC, live state as of
2026-08-02: balance=$1965.78, n_closed=27, win_rate=40.7%, realized=-$34.22).

Zones are still detected on the FULL price history (structure needs prior
context), but only entries whose entry_ts falls inside [launch, now] are
counted -- same "detect on full window, bucket candidates by entry_ts"
pattern the train/val/holdout scripts already use, applied to one custom
window instead of three fixed-fraction ones.
"""
import sys

sys.path.insert(0, "/Users/styserg/Desktop/Tyagach/src")
from portfolio import stats
from tyagach_samedir_ab import simulate_tagged
import simulate_since_inception as sim

LAUNCH_TS_MS = 1782424743912  # bot's actual started_at_ms (2026-06-25 21:59:03 UTC)
NOW_TS_MS = 1785672900000     # last_processed_ts_ms as of this session (2026-08-02 12:15 UTC)

# Actual live realized result over this exact window, for comparison
LIVE_ACTUAL = dict(balance=1965.78, n_closed=27, win_rate=0.4074, realized_pnl=-34.22)


def main():
    print(f"launch={LAUNCH_TS_MS}  now={NOW_TS_MS}  "
          f"span_days={(NOW_TS_MS - LAUNCH_TS_MS) / 1000 / 86400:.1f}\n")

    all_candidates = []
    per_cell_counts = {}
    tf_cache = {}
    for (tf, kind), cfg in sim.ACTIVE_CELLS.items():
        if tf not in tf_cache:
            df = sim.structure.load_csv(sim.TF_FILES[tf])
            n = len(df)
            ts = df["ts_ms"].values
            iv_series = sim.load_dvol_aligned(sim.DVOL_JSON, df)
            o, h, l, c = df["open"].values, df["high"].values, df["low"].values, df["close"].values
            tf_cache[tf] = (df, n, ts, iv_series, o, h, l, c)
        df, n, ts, iv_series, o, h, l, c = tf_cache[tf]
        cell_cands = sim.build_candidates(kind, tf, cfg, df, n, ts, iv_series, o, h, l, c)
        # restrict to entries inside the live bot's actual runtime window
        windowed = [(tf_, cand) for tf_, cand in cell_cands
                    if LAUNCH_TS_MS <= cand.entry_idx < NOW_TS_MS]
        per_cell_counts[f"{tf}/{kind}"] = len(windowed)
        all_candidates.extend(windowed)

    print(f"{'cell':<10} {'n_signals_in_window':>20}")
    for label, n_sig in sorted(per_cell_counts.items()):
        print(f"{label:<10} {n_sig:>20}")
    print(f"{'TOTAL':<10} {len(all_candidates):>20}\n")

    span_days = (NOW_TS_MS - LAUNCH_TS_MS) / 1000 / 86400

    final, curve, closed = simulate_tagged(all_candidates, "per_tf")
    s = stats(sim.STARTING_BALANCE, final, curve, len(closed))
    wins = sum(1 for pnl in closed if pnl > 0)
    win_rate = wins / len(closed) if closed else 0.0
    tpd = len(closed) / span_days if span_days > 0 else 0.0

    print("=== U4 config replayed over the bot's actual runtime window (uncapped) ===")
    print(f"n_closed={s['n_closed']}  trades/day={tpd:.2f}  win_rate={win_rate:.1%}")
    print(f"final_balance=${s['final_balance']:.2f}  total_return={s['total_return_pct']:+.1f}%  "
          f"max_dd={s['max_dd_pct']:.1f}%  calmar={s['calmar']:.2f}")

    print("\n=== Capacity-cap sensitivity ===")
    print(f"{'cap':>10} {'n_closed':>9} {'trades/day':>10} {'win_rate':>9} {'final_$':>12} {'return%':>10} {'maxDD%':>7}")
    for cap in [None, 10_000, 5_000, 2_000]:
        final_c, curve_c, closed_c = sim.simulate_capacity(all_candidates, cap)
        s_c = stats(sim.STARTING_BALANCE, final_c, curve_c, len(closed_c))
        wins_c = sum(1 for pnl in closed_c if pnl > 0)
        wr_c = wins_c / len(closed_c) if closed_c else 0.0
        tpd_c = len(closed_c) / span_days if span_days > 0 else 0.0
        cap_label = "uncapped" if cap is None else f"${cap:,}"
        print(f"{cap_label:>10} {s_c['n_closed']:>9} {tpd_c:>10.2f} {wr_c:>9.1%} "
              f"{s_c['final_balance']:>12,.2f} {s_c['total_return_pct']:>+10.1f} {s_c['max_dd_pct']:>7.1f}")

    print("\n=== vs. actual live result over the same window ===")
    print(f"{'':>12} {'balance':>12} {'n_closed':>9} {'win_rate':>9}")
    print(f"{'LIVE actual':>12} {LIVE_ACTUAL['balance']:>12,.2f} {LIVE_ACTUAL['n_closed']:>9} {LIVE_ACTUAL['win_rate']:>9.1%}")
    print(f"{'replay@$2k cap':>12} {'see above':>12}")


if __name__ == "__main__":
    main()
