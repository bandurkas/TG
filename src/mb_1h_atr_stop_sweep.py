from __future__ import annotations
"""1h/MB ATR-adaptive stop: does giving 1h/MB the same ATR-scaled stop buffer
already used by 1h/OB and 1h/FVG (tp_retarget_sweep.ATR_BUFFER_MULT, currently
only {("1h","OB"): 2.0, ("1h","FVG"): 2.0}) fix its outsized backtest SL rate
(26.2% vs 8.1%/8.2% for 1h/OB/1h/FVG at comparable-or-smaller r_target,
despite 1h/MB's stop distance being roughly in the same range empirically)?

1h/MB currently has no ATR_BUFFER_MULT / FLAT_BUFFER_FRAC_OVERRIDE entry, so
find_entry() falls through to DEFAULT_FLAT=0.0015 (0.15% of zone midpoint) --
the tightest, least volatility-aware buffer available, unlike its 1h OB/FVG
siblings which already widen/narrow with realized vol via ATR.

Mechanic: monkeypatch tp_retarget_sweep.ATR_BUFFER_MULT with a
("1h","MB"): atr_mult entry for the duration of each eval -- find_entry()
already branches on this dict (see tp_retarget_sweep.py:88-92), so this
reuses the exact live entry/stop logic unchanged, no duplicated code path.
Baseline = current live flat 0.15% (dict entry absent).

Run: python3 mb_1h_atr_stop_sweep.py
"""
import sys

sys.path.insert(0, "/Users/styserg/Desktop/Tyagach/src")
import tp_retarget_sweep as base
from tp_retarget_sweep import build_candidates, load_tf, run_splits, run_quarters, N_QUARTERS

TF, KIND = "1h", "MB"
CFG = dict(kind="MB", depth_frac=0.400, r_target=10.0, expiry_days=0.25)  # == live tyagach/services/config.py
ATR_GRID = (0.5, 0.75, 1.0, 1.5, 2.0, 2.5, 3.0, 4.0)


def eval_variant(atr_mult):
    orig = base.ATR_BUFFER_MULT.get((TF, KIND))
    if atr_mult is None:
        base.ATR_BUFFER_MULT.pop((TF, KIND), None)
    else:
        base.ATR_BUFFER_MULT[(TF, KIND)] = atr_mult
    try:
        df, n, ts, iv_series, o, h, l, c, atr = load_tf(TF)
        cands, meta = build_candidates(TF, KIND, CFG, df, n, ts, iv_series, o, h, l, c, atr, CFG["r_target"])
    finally:
        if orig is None:
            base.ATR_BUFFER_MULT.pop((TF, KIND), None)
        else:
            base.ATR_BUFFER_MULT[(TF, KIND)] = orig
    splits = run_splits(cands, ts)
    rets, dds = run_quarters(cands, ts)
    n_negative = sum(1 for r in rets if r < 0)
    return dict(splits=splits, meta=meta, n_negative=n_negative, worst_q=min(rets), maxdd_q=max(dds))


def print_row(label, r):
    v, h = r["splits"]["validation"], r["splits"]["holdout"]
    m = r["meta"]
    n_tot = m["n_tp"] + m["n_sl"] + m["n_expiry"]
    sl_rate = m["n_sl"] / n_tot * 100 if n_tot else 0
    print(f"{label:<16} n={n_tot:>5} sl_rate={sl_rate:>5.1f}% win={m['win_rate']*100:>5.1f}% "
          f"val_ret={v['total_return_pct']:>+8.1f} val_calmar={v['calmar']:>7.2f} "
          f"hold_ret={h['total_return_pct']:>+8.1f} hold_calmar={h['calmar']:>7.2f} "
          f"q_neg={r['n_negative']}/{N_QUARTERS} q_worst={r['worst_q']:>+6.1f} q_maxdd={r['maxdd_q']:>5.1f}")


def main():
    baseline = eval_variant(None)  # current live: flat 0.15%
    print_row("LIVE(flat0.15%)", baseline)
    for mult in ATR_GRID:
        r = eval_variant(mult)
        print_row(f"ATR x{mult}", r)


if __name__ == "__main__":
    main()
