# Tyagach — architecture (step 1 of workflow, before any code)

Decisions locked in this session (2026-06-26). Don't re-litigate without new evidence.

## Execution model

Real Bybit **testnet** orders — not a pure internal simulator. This means the
bot is exposed to real bid/ask spread, real fills, real option chain
availability, which is the #1 gap flagged in TYAGACH_HANDOFF.md's "not
validated" section.

**Account: reuses Grogu1's existing API key** (not a new dedicated account as
originally planned). Decrypted from opt-app's Postgres (account_id=3) and
written to `/root/tyagach/.env` (gitignored). **Accepted consequence:**
Grogu1's own `reconcile.py` will see Tyagach's ETH option positions as
"untracked" on the shared account and will likely block Grogu1's new opens —
this is the exact landmine documented in
`feedback_options_live_two_accounts` memory, deliberately triggered here on
user's explicit call ("приносим в жертву Grogu", 2026-06-26). Not a bug to
fix; Grogu1 getting blocked is an accepted side effect, not a regression to
chase.

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

## Next (step 2 of workflow: code)

1. New testnet Bybit API key (user to create on testnet.bybit.com, OptionsTrade perm).
2. Scaffold `/root/tyagach` repo structure: `src/` (ported detectors+portfolio),
   `services/loop.py`, `services/api.py`, `db/schema.sql` + `db/repo.py`, `docker-compose.yml`.
3. Wire live klines/DVOL fetchers (adapt existing `fetch_klines.py`/`fetch_dvol.py`).
4. Implement SELL-only execution against Bybit testnet options endpoints.
5. Mission Control 4th panel.

Per [[feedback_options_workflow_order]]: code review mandatory before test,
review again before any deploy — including the paper/testnet deploy itself.
