# Tyagach — architecture (step 1 of workflow, before any code)

Decisions locked in this session (2026-06-26). Don't re-litigate without new evidence.

## Execution model

Real Bybit account, real quotes/instruments — not a pure internal simulator.
**Turned out to be a REAL Bybit mainnet account, not Bybit's separate
testnet.bybit.com environment** (the original plan). Briefly used Grogu1's
existing key on 2026-06-26 (decrypted from opt-app's Postgres, account_id=3)
and accepted the consequence that Grogu1's own `reconcile.py` would see
Tyagach's positions as "untracked" and block its opens — superseded same day
once the user provided a fresh, separate API key. That key authenticates to
`api.bybit.com` (mainnet), confirmed via `get_api_key_information` (UTA=1,
`Options: OptionsTrade` permission, $0 balance — unfunded).

**Paper/live gate added because of this** (`services/config.py`
`TRADING_MODE`, default `"paper"`): the real account is used for read-only
market data (instruments/quotes/wallet) in both modes, but
`execution.py`'s `sell_to_open`/`buy_to_close` only call Bybit's real
`place_order` when `TYAGACH_TRADING_MODE=live` — otherwise they simulate a
fill against the same live quote a real order would have used
(`_paper_fill`), so nothing touches the real account's positions/balance
until the user explicitly arms it. `/root/tyagach/.env` (gitignored) holds
`BYBIT_TESTNET=false` + `TYAGACH_TRADING_MODE=paper`. User's plan: flip to
`live` once the paper run looks right. Grogu1 is no longer touched at all.

## Repo / deployment

Fully separate service, own git history (`git@github.com:bandurkas/TG.git`,
cloned to `/root/tyagach` on VPS3) — NOT folded into the `opt-app` monolith.
No shared Postgres, no shared credentials store, no shared control_repo.

Two long-running processes, docker-compose, sharing one SQLite file via volume:

| Service | Role |
|---|---|
| `tyagach_loop` | Wakes periodically, pulls latest ETH 15m klines (Bybit) + DVOL (Deribit) directly — own fetchers, no dependency on opt-app's poller. Runs zone detection (`structure/ob/bb/mb/zones`), evaluates entries via `portfolio.py`'s engine, places/manages real testnet orders, writes positions/equity to SQLite. |
| `tyagach_api` | FastAPI, reads SQLite (read-only), exposes `/api/v1/tyagach/{state,positions,equity_history}` + pause/close-all control endpoints. Listens on `0.0.0.0:8100` (same exposure pattern opt-app already uses on 3000/8000 — accepted risk, not hardened behind a proxy for now). |

## Storage

SQLite file inside the repo's data volume — no Postgres. Tables mirror the
`eth_straddle_*` shape conceptually (positions, equity_snapshots, bot_state)
but namespaced for Tyagach's 3 zone types (OB/BB/MB), not a single strategy.

## Mission Control integration

MC's existing Next.js frontend adds a 4th panel, fetching directly from
`http://<vps3>:8100/api/v1/tyagach/*` (cross-service call, bypasses MC's own
auth gate for this one panel — same accepted tradeoff as the open-port
decision above). Pause/close-all buttons on that panel call Tyagach's own
endpoints, not opt-app's `control_repo`.

## Code provenance

Port (don't import live) from `~/Desktop/smc_options/src/`:
`structure.py, ob.py, bb.py, mb.py, zones.py, bs_pricer.py, dvol.py,
fetch_klines.py, fetch_dvol.py, portfolio.py`. Research stays read-only in
smc_options; Tyagach gets its own copies so research and live code don't
share a dependency edge (same isolation pattern as every other bot here).

## What's explicitly deferred / accepted as risk

- No reverse-proxy/auth hardening on Tyagach's API port — matches existing
  opt-app exposure, not a regression, but also not fixed.
- No cross-zone/cross-strategy collateral checks beyond what `portfolio.py`
  already does internally.
- Real testnet execution will surface fill/spread behavior the 4yr backtest
  never modeled (see handoff doc point 3) — expect live numbers to diverge
  from the 39% APR backtest figure; that figure is a ceiling, not a target.

## Code review findings (2026-06-26) — fixed vs. accepted

Full code review run before any testnet execution (8 finder angles + verify).
**Fixed in the same pass:**
- `execution.py`: `_TERMINAL` didn't include `PartiallyFilled` — orders could
  partially fill on the exchange while the poller gave up and treated it as
  unfilled, leaving an untracked exchange position.
- `loop.py` `_execute_close`: `market_data.get_spot_price()` had no
  try/except — a transient failure AFTER a confirmed real buy-to-close fill
  would leave the position stuck "open" in SQLite forever. Now caught,
  falls back to 0.0, close is always recorded.
- `api.py`: now calls `repo.init_db()` defensively at import time (idempotent)
  — docker-compose's `depends_on` only waits for container start, not for
  the loop container's own `init_db()` to finish, so the API could otherwise
  read an empty `bot_state` row on a cold start race.
- `signal_engine.py` `sync_new_zones`: fixed an off-by-one — `valid_from` was
  set to the zone's own formation bar instead of `formed_idx + 1` (the
  research convention, `zones.py::build_zones`), letting entries trigger one
  bar earlier than anything the 4yr backtest validated. Confirmed via smoke
  test: trigger count on the same 2000-bar window dropped from 110/110 to
  98/110 after the fix — the expected direction of change.
- `loop.py`: restructured `process_new_bar` to walk each newly-closed bar
  chronologically (was: only ever looked at the single latest closed bar,
  silently skipping SL/TP hits on intermediate bars after a restart/outage
  gap) and to process exits before entries within each bar (was: entries
  were decided off a balance/open-positions snapshot taken before that bar's
  exits ran, one tick stale — diverged from the backtest's `simulate()`,
  which purges exits before evaluating each new candidate). Also fixed
  `entry_ts_ms` being recorded as the tick's latest-bar timestamp instead of
  the signal's actual trigger-bar timestamp, and added a log line for the
  previously-silent "sizing rounds to 0 qty" skip path.

**Accepted, not fixed (logged so they don't get silently lost):**
- `execution.py` has no LIMIT→MARKET fallback sweep (opt-app's
  `execution.py` does, with reduce-on-reject sizing). An order that can't
  fill IOC is just skipped and retried next bar — safe (no orphaned
  position) but a weaker fill guarantee than opt-app's pattern. Revisit if
  testnet shows frequent no-fills.
- `api.py`'s `close_all` is signal-level only (invalidates pending zone
  signals, pauses new entries) — does NOT flatten already-open real option
  positions. Already disclosed in the endpoint's own docstring/response.
- `find_instrument` anchors its expiry search to wall-clock `time.time()`
  rather than the signal's bar timestamp — only matters when processing a
  multi-bar backlog after an outage, where it's a minor approximation.
- `market_data.get_klines()` re-fetches the full ~2000-bar rolling window on
  every 60s poll tick even though decisions only act once per new 15m close
  (~15x more network calls than strictly needed) — real but non-urgent
  inefficiency for a paper/testnet bot; revisit if Bybit rate limits bite.
- `core/bs_pricer.py` was ported but is currently unused (live execution
  prices off real Bybit quotes, not a BS model) — kept for potential future
  analytics, not dead-code-deleted.

## Amendment (2026-07-03): cell/threshold admission criterion

The 2026-07-03 IV-threshold session (commit `205ac32`) picked
`ACTIVE_CELLS` and per-kind `iv_threshold` by reading only the **holdout**
column of `results/sweep_iv_lower_multitf.csv` (the sweep script's own
verdict logic filters `split == "holdout"` before printing). This skipped
the review step entirely — went straight from sweep → config edit → deploy,
no review commit (unlike the initial build's `1940be8`), no
`ARCHITECTURE.md` update. Re-reading all three splits (train/validation/
holdout) surfaced: `15m/OB` negative on train AND validation (validation
n=700+, not small-sample noise), only positive on holdout; `30m/MB` and
`30m/BB` (both added this session) negative on validation, positive on
train+holdout.

**Rule going forward:** a (tf, kind, iv_threshold) cell is only admitted
into `ACTIVE_CELLS` if `avg_net_$` (fee-adjusted) is positive in **all
three** splits — train, validation, and holdout — not holdout alone.
Holdout-only agreement with a negative validation is treated as likely
overfit/noise, not edge. This mirrors the walk-forward-consistency bar
already applied to Sniper1/Boba1 (see [[feedback_backtest_methodology_pitfalls]]
and [[feedback_check_backtest_population_before_deploy]]) — Tyagach's
config tuning had not been held to the same bar until this amendment.

Applying this bar removes `("15m","OB")` (pre-existing, negative on
train+validation, never previously re-validated), `("30m","MB")` and
`("30m","BB")` (both added same-day on 2026-07-03, negative on validation)
from `ACTIVE_CELLS`. `("2h","MB")` — this session's strongest genuine find —
passes on all 3 splits and stays. `("1h","OB")` only passes at iv≥75 with
n=13 on validation, too thin to trust; stays excluded.

**Full portfolio-level validation of the deployed config (2026-07-03, `src/tyagach_portfolio_multitf.py`):**
The per-cell sweep above only checks each cell's own avg_net_$ in isolation —
it doesn't capture compounding or the real concurrency caps
(`MAX_OPEN_PER_ZONE`, `MAX_OPEN_TOTAL_GLOBAL=8`, same-direction blocking,
BB>MB>OB priority) that the live bot actually enforces across all 6 active
cells sharing one balance. Re-ran the same event-driven engine
(`portfolio.py`'s `Candidate`/`PortfolioConfig`/`simulate`/`stats`) that
produced the original single-TF 39.6% APR figure in `TYAGACH_HANDOFF.md`,
this time merging candidates from all 4 TFs on one absolute-timestamp
timeline (not each TF's own positional bar index) so cross-TF concurrency
resolves correctly. Result — `results/tyagach_portfolio_multitf_confirmed.csv`:

| split | days | n_closed | trades/day | return | approx APR | maxDD | Calmar |
|---|---|---|---|---|---|---|---|
| train | 882 | 2521 | 2.86 | +895.7% | +158.7% | 15.0% | 59.6 |
| validation | 294 | 1205 | 4.10 | +62.0% | +82.0% | 11.4% | 5.5 |
| holdout | 294 | 1048 | 3.56 | +117.8% | +162.7% | 10.3% | 11.5 |

Positive on all 3 splits (passes the amendment's own admission bar, applied
at the portfolio level this time) — no train/validation/holdout sign flip
like the per-cell issue above. Trades/day (2.9-4.1) lands close to the
original single-TF estimate (~2.7/day, from the "~80/month" figure in
`TYAGACH_HANDOFF.md`), confirming the multi-TF + threshold-lowering +
cell-correction work did not undershoot that baseline. approx APR is
annualized simple compounding on the split's own return — **not a real
forward-looking target**: no bid-ask spread/slippage modeled (only the flat
Bybit fee schedule), and `weight_pct`-of-balance sizing compounding
indefinitely will eventually hit real option-market liquidity limits well
before scaling to the balances implied by these returns. Treat as "this
config is structurally profitable and internally consistent," not as an
APR forecast — same caveat as every other backtest number in this repo
(see [[feedback_edge_eval_vs_opportunity_cost]]).

**Follow-on research (2026-07-03, same day): 3 directions, in order**

1. **Sizing/caps grid search** (`src/tyagach_portfolio_gridsearch.py`,
   `results/tyagach_gridsearch_*.csv`) — swept `weight_pct`/`max_open_per_zone`/
   `MAX_OPEN_TOTAL_GLOBAL` around the live values on the 6-cell multi-TF
   candidate set. Finding: `total_return_pct` increases **monotonically**
   with every weight parameter, with no interior optimum, and the per-zone/
   global caps never bound the top results (identical stats at cap=4 and
   cap=12). This is the classic unconstrained-compounding artifact — with
   percent-of-balance sizing over thousands of trades, "size bigger" always
   wins in a backtest that has no market-depth/capacity ceiling. Confirms
   (doesn't newly discover) why `WEIGHT_PCT` in `config.py` is already
   commented "manually chosen (NOT the raw grid-search optimum)". **No change
   made** — the live weights are correct as-is; the grid search is not a
   usable tool for this question without a capacity constraint the codebase
   doesn't model.
2. **Per-cell `CELL_CONFIG` rebuild** — deferred, per the user's own standing
   rule in [[project_tyagach_engine_rebuild]] (wait for 20-30 live cycles per
   cell; currently 6 total live trades). Not attempted.
3. **5m TF re-check** (`src/tyagach_5m_revisit.py`,
   `results/sweep_5m_revisit.csv`) — previously excluded as "gross < fee" at
   the old higher thresholds; re-ran at the new lower thresholds [50-75] with
   the same all-3-splits-positive bar applied from the start. OB negative on
   train+validation+holdout at every threshold; BB negative almost
   everywhere; MB near-breakeven on train/holdout but negative on validation
   at every threshold (same failure pattern as `15m/OB`/`30m/MB` above).
   **Stays excluded** — confirmed under the corrected methodology, not just
   inherited from the old one.

**Review pass #2 caught a real bug this change would have silently missed:**
`signal_engine.sync_new_zones` already gated zone *creation* on
`config.ACTIVE_CELLS`, but `scan_pending_zones` (the trigger path) evaluated
every `status='pending'` row for the TF with no such check — a zone_signal
row created while a cell was active would keep triggering trades after that
cell was removed from `ACTIVE_CELLS`. At review time there were 3 such rows
live (`15m/OB` ×2, `30m/MB` ×1). Fixed: `scan_pending_zones` now expires any
pending row whose `(tf, kind)` isn't in `ACTIVE_CELLS` before evaluating it.
Regression test: `tests/test_active_cells_filter.py`.

## Amendment (2026-07-04): realtime SL/TP sweep

Found live: a `1h/MB` position sat open ~85 minutes after spot traded ~$20
past its TP, because the per-TF bar-close exit check (`_process_tf`) only
fires when THAT position's own TF closes a bar — up to a full bar width of
detection lag (worst case) for slower TFs (2h → up to 2h). Manually closed
that position via `loop._execute_close` at the live quote (net +$6.93).

Fix: `loop._sweep_realtime_exits()`, called every tick alongside
`_sweep_real_expiry()`, checks SL/TP for ALL open positions against the
current live spot (`market_data.get_spot_price()`), reusing
`portfolio_state.check_exits(all_open, spot, spot, now_ms)` — same function,
just called with a single point instead of a bar's high/low. Cuts worst-case
detection lag from up to one bar width down to ~`POLL_SECONDS` (60s).

Why this doesn't require re-validating the backtested edge: `_execute_close`
already fills against a LIVE quote fetched at close time, not the historical
bar's close price — the bar's high/low was only ever used as the *trigger*
condition, never the fill price. Checking that same trigger condition more
often only tightens reaction latency; it doesn't change what triggers a
close or what price it fills at. Entry/signal logic (zone detection,
r_target, expiry, IV threshold) is untouched. The per-TF bar-close check
still runs too — redundant in the common case (position usually already
closed by the realtime sweep) but kept as a safety net for a failed spot
fetch, and to match how each cell's exit was originally backtested.
Regression test: `test_point_check_*` in `tests/test_per_tf_exits.py`.

## Next (step 2 of workflow: code)

1. New testnet Bybit API key (user to create on testnet.bybit.com, OptionsTrade perm).
2. Scaffold `/root/tyagach` repo structure: `src/` (ported detectors+portfolio),
   `services/loop.py`, `services/api.py`, `db/schema.sql` + `db/repo.py`, `docker-compose.yml`.
3. Wire live klines/DVOL fetchers (adapt existing `fetch_klines.py`/`fetch_dvol.py`).
4. Implement SELL-only execution against Bybit testnet options endpoints.
5. Mission Control 4th panel.

Per [[feedback_options_workflow_order]]: code review mandatory before test,
review again before any deploy — including the paper/testnet deploy itself.
