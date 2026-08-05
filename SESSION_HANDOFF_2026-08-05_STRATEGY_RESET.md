# Session handoff — 2026-08-05 — strategy reset (deployed)

Continues `SESSION_HANDOFF_2026-08-05_MB_REVERSAL_IDEA.md` (MB reversal
REJECTED). User then asked to fully rethink the strategy so USDT grows
instead of shrinks, explicitly authorizing autonomous decisions + a live
(paper) deploy without further check-ins. This file records what was found
and shipped.

## 1. What was live-losing, and why "just tune more" was the wrong next move

Live paper balance: $1914.59 from $2000 start (-4.27%), 47 closed trades over
40 days (2026-06-25 → 2026-08-05). Meanwhile `tyagach/services/config.py`'s
own comments claim the current 12-cell config backtests at validation
+425%→+463%, holdout +433%→+448%, 0/8 negative quarters. That gap is the
real story this session, not another parameter sweep.

**Two independent problems found, both now addressed:**

### A. Position sizing was uncapped compounding
`budget = weight_pct * balance` scales with the CURRENT (simulated) balance.
Over a multi-year backtest replay that claims +400%+, the simulated balance
compounds into the tens of thousands, so late-dated signals get sized many
times larger than the same signal would be today — inflating the headline
return numbers into something that says almost nothing about real per-trade
edge. Confirmed with `src/overfit_capacity_check.py`: capping sizing at the
$2,000 starting balance collapses the SAME validation/holdout split's claimed
return from +2,390%/+1,274% to +309%/+262% (uncapped since-inception: a
mechanically absurd +12,215,335,829%).

**Fix shipped:** `config.SIZING_BALANCE_CAP` (default $2,000, env override
`TYAGACH_SIZING_BALANCE_CAP`) — `services/portfolio_state.py` now sizes off
`min(balance, cap)`. Doesn't change anything today (live balance is still
below $2,000) but prevents silent runaway sizing the moment the bot starts
winning, which matters a lot more once this goes anywhere near real money.

### B. Live fires ~16x fewer entries than the backtest predicts for the SAME calendar window (bigger effect)
Replayed the current 12-cell config against the bot's own historical price
data for its actual live window (2026-06-25 → 2026-08-03, `eth_*.csv`
extends to 08-03 10:30 UTC):
- Zone **detection** matches almost exactly: 1,669 zones (backtest replay)
  vs 1,680 (live `zone_signals` table) — the detectors are not the problem.
- Zone **conversion to a trade** does not match at all: backtest converts
  ~45% of zones into portfolio-accepted trades (755/1,669); live converts
  ~3% (50/1,680, and 50 triggered ≈ 47 closed + 3 open — matches exactly).
- Same-window backtest return: **+4.5%** (not +300%+ — that figure only
  shows up over cherry-picked multi-year stretches elsewhere in the 4y
  history, i.e. it isn't a reliable "this is what a normal 40 days looks
  like" number either). Live: **-4.27%**. A ~9pp gap on top of a 16x
  frequency gap is far more consistent with "live sees a small, unlucky
  fraction of the model's trades" than with "the whole edge is fake" —
  though see the caution in §3.

Root cause identified in `tyagach/services/signal_engine.py`'s
`scan_pending_zones` (line ~173, unchanged by this session): a `stale_after`
guard (as tight as **1 bar / ~1-2h** for 1h/2h cells) silently expires any
touch+rejection whose bar is more than `stale_after` bars behind the CURRENT
last bar — this exists specifically to avoid trading a cold-start backlog
after downtime (own code comment: *"e.g. a cold-start backlog"*), but:
1. it does not exist in ANY backtest/research script at all (`find_entry` in
   `tp_retarget_sweep.py` and everywhere downstream of it has no equivalent
   check) — so every "validated" backtest number implicitly assumes this
   constraint away, and
2. all THREE distinct `expired` causes (genuine lookahead timeout /
   stale-after-restart / entry-hour veto) collapsed into the same `status=
   'expired'` value in the DB, so there was no way to see which one was
   actually responsible for the 1,289 (of 1,680) expired live signals.

**Fix shipped (observability only, not a behavior change):**
`zone_signals.expire_reason` (new nullable column, additive migration in
`db/repo.py::_ensure_columns`) records which of `lookahead_timeout` /
`stale_touch` / `entry_veto_hour` / `inactive_cell` fired, at all 6 call
sites (`signal_engine.py` x4, `loop.py::_expire_orphaned_pending_signals`,
`repo.py::_expire_legacy_zone_keys` not touched — legacy-key-format edge
case, out of scope). **Deliberately did NOT loosen `stale_after`** — it's a
real, reasonable risk control (don't trade a touch against a price context
that's no longer current) and this session found no backtest evidence either
way on what the right value is, only that its cost has never been measured.
Once a few days of live data accumulate with the reason column populated,
the actual `stale_touch` vs `lookahead_timeout` vs `entry_veto_hour` split
will be visible for the first time — check `zone_signals.expire_reason`
distribution before touching `stale_after` again.

## 2. MB deactivated (again)

Two independent, converging pieces of evidence from this session:
- Live since the 2026-08-02 rejection-close reactivation: MB is 5/5 losing
  trades, -$26.99; MB overall (including pre-reactivation history) is
  -$64.81 of the bot's total -$85.41 loss — almost the entire deficit.
- The "reverse MB" idea (sell the opposite option side on the same signal,
  mirroring stop/tp around entry) was fully speced, implemented
  (`src/mb_reversal_sweep.py`, self-checked: mirror is a verified involution,
  `reverse=False` reproduces the baseline byte-for-byte), and **decisively
  REJECTED** — not just unprofitable but near-total ruin on every cell and
  r_target tested (calmar ≈ -1.0, 8/8 negative quarters). Mechanism: MB's
  barriers are asymmetric (close stop / far r_target, up to 10x on 2 of 4
  cells) — mirroring around entry swaps which barrier is close and which is
  far, so the reversed bet's "easy" barrier is now its stop, not its tp. The
  original "SL-rate > 50% ⇒ mirror must be profitable" intuition only holds
  for symmetric payoffs; it doesn't apply here.

`("15m"|"30m"|"1h"|"2h", "MB")` removed from `config.ACTIVE_CELLS`.
`CELL_CONFIG`/`ZONE_CONFIG["MB"]` values were NOT deleted (same convention as
the 2026-07-07 deactivation) so a future reactivation with a genuinely new
mechanism can reuse them.

## 3. Honest caveats — do not over-read this as "fixed"

- The frequency-gap finding (§1B) explains a large PORTION of the backtest/
  live divergence, but the remaining ~9pp gap on the SAME window (backtest
  +4.5% vs live -4.27%) is still just as consistent with "47 trades is too
  small a sample to know the sign of the edge yet" as with "there's a real,
  smaller edge and live got unlucky." Neither is resolved by this session.
- This session did NOT re-validate the surviving 8 cells' backtest numbers
  against the same rigor applied to MB/reversal — the +400%-class portfolio
  claims in `config.py`'s older comments should be read as unreliable
  (per §1A) for ALL cells, not just MB. No claim here that the 8 remaining
  cells have proven positive edge — only that MB is the one cell with two
  independent, converging pieces of NEGATIVE evidence, which is a much
  lower bar to act on than "prove this is profitable."
- **The only trustworthy signal going forward is live paper results**, now
  that `expire_reason` will show whether entry frequency actually recovers
  once a `stale_touch`/`lookahead_timeout`/`entry_veto_hour` breakdown is
  available (check after ~1 week) — not another historical-CSV backtest.

## 4. What shipped (commit — see `git log`)

- `tyagach/services/config.py`: MB removed from `ACTIVE_CELLS`;
  `SIZING_BALANCE_CAP` added (default $2,000).
- `tyagach/services/portfolio_state.py`: sizing now uses
  `min(balance, config.SIZING_BALANCE_CAP)`.
- `tyagach/db/repo.py`: `expire_reason` column (additive migration),
  `set_zone_signal_status(..., reason=None)`.
- `tyagach/services/signal_engine.py`, `tyagach/loop.py`: all `expired`
  call sites now pass a `reason`.
- `tyagach/tests/test_mb_ob_reactivation.py`: MB-active test flipped to
  MB-inactive (kept the cell_config-values test unchanged).
- `tyagach/tests/test_strategy_reset_2026_08_05.py`: new, covers all 4
  `expire_reason` values + sizing-cap behavior (capped and below-cap cases).
- Research (not deployed, reference only): `src/mb_reversal_sweep.py`,
  `src/overfit_capacity_check.py`.
- All 123 tests pass (`cd tyagach && python3 -m pytest tests/ -q`).
- Deployed to VPS3 (`/root/tyagach/tyagach`, `docker compose build && up -d
  --force-recreate`) — verify commit hash and container health after deploy.

## 5. Next session TODO (priority order)

1. **After ~1 week live**: query `zone_signals.expire_reason` distribution
   (`GROUP BY expire_reason`). If `stale_touch` dominates, that's a strong
   case for loosening `stale_after` (with a backtest-informed value, not a
   guess) or restructuring the scan loop to resume from last-checked-bar
   instead of always re-scanning from `valid_from`. If `lookahead_timeout`
   dominates, the frequency gap is more structural (zones just don't get
   touched as often as the backtest implies) and `stale_after` isn't the
   lever.
2. Watch live entries/day post-MB-deactivation and post-sizing-cap (should
   be unaffected day-to-day, this is a forward-looking guard) — confirm no
   regression vs the pre-change baseline.
3. Given §3's caveat, do NOT treat the 8 surviving cells' old +400%-class
   backtest numbers as validated. If revisiting any of them, first re-run
   under `config.SIZING_BALANCE_CAP`-equivalent fixed sizing before trusting
   the result, same discipline now applied to MB/reversal this session.
