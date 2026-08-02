from __future__ import annotations
"""What-if, restricted to the bot's ACTUAL live runtime window, using the
FULL proposed 13-cell config (U1-U4 + this round's 15m/OB re-add + 4x MB)
and CORRECTED (through-today) price/DVOL data -- supersedes
simulate_since_launch.py, which used the stale data (cut off 2026-06-27)
and only the 8-cell U4 config, not the 13-cell set actually being shipped.

Bot's real launch: started_at_ms=1782424743912 (2026-06-25 21:59 UTC).
"Now" = the freshly-fetched data's actual end (2026-08-02), not a stale
guess -- computed from the data itself.
"""
import sys

sys.path.insert(0, "/Users/styserg/Desktop/Tyagach/src")
from portfolio import stats
from tyagach_samedir_ab import simulate_tagged
import u_all13_combo_validation as sim

LAUNCH_TS_MS = 1782424743912  # bot's actual started_at_ms (2026-06-25 21:59:03 UTC)

# Actual live realized result over this exact window, for comparison
LIVE_ACTUAL = dict(balance=1965.78, n_closed=27, win_rate=0.4074, realized_pnl=-34.22)


def main():
    tf_cache = {}
    print("Building candidates for PROPOSED 13-cell set on CORRECTED (through-today) data...")
    all_candidates = sim.build_all(sim.PROPOSED_CELLS, tf_cache)

    now_ts_ms = int(tf_cache["15m"][2][-1])  # freshest available bar, not a guess
    span_days = (now_ts_ms - LAUNCH_TS_MS) / 1000 / 86400
    print(f"launch={LAUNCH_TS_MS}  now={now_ts_ms}  span_days={span_days:.1f}\n")

    windowed = [(tf_, cand) for tf_, cand in all_candidates
                if LAUNCH_TS_MS <= cand.entry_idx < now_ts_ms]
    print(f"total signals in window (13-cell, pre-slot-competition): {len(windowed)} "
          f"(of {len(all_candidates)} over full history)\n")

    final, curve, closed = simulate_tagged(windowed, "per_tf")
    s = stats(sim.STARTING_BALANCE, final, curve, len(closed))
    wins = sum(1 for pnl in closed if pnl > 0)
    win_rate = wins / len(closed) if closed else 0.0
    tpd = len(closed) / span_days if span_days > 0 else 0.0

    print("=== 13-cell config replayed over the bot's actual runtime window (corrected data) ===")
    print(f"n_closed={s['n_closed']}  trades/day={tpd:.2f}  win_rate={win_rate:.1%}")
    print(f"final_balance=${s['final_balance']:.2f}  total_return={s['total_return_pct']:+.1f}%  "
          f"max_dd={s['max_dd_pct']:.1f}%  calmar={s['calmar']:.2f}")

    print("\n=== vs. actual live result over the same window ===")
    print(f"{'':>16} {'balance':>12} {'n_closed':>9} {'win_rate':>9}")
    print(f"{'LIVE actual':>16} {LIVE_ACTUAL['balance']:>12,.2f} {LIVE_ACTUAL['n_closed']:>9} {LIVE_ACTUAL['win_rate']:>9.1%}")
    print(f"{'13-cell replay':>16} {s['final_balance']:>12,.2f} {s['n_closed']:>9} {win_rate:>9.1%}")


if __name__ == "__main__":
    main()
