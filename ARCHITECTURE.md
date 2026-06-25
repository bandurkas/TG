# Tyagach — architecture (step 1 of workflow, before any code)

Decisions locked in this session (2026-06-26). Don't re-litigate without new evidence.

## Execution model

Real Bybit **testnet** orders, on a **new dedicated testnet account** (separate
from Boba1/Grogu1/Sniper1) — not a pure internal simulator. This means the bot
is exposed to real bid/ask spread, real fills, real option chain availability,
which is the #1 gap flagged in TYAGACH_HANDOFF.md's "not validated" section.

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

## Next (step 2 of workflow: code)

1. New testnet Bybit API key (user to create on testnet.bybit.com, OptionsTrade perm).
2. Scaffold `/root/tyagach` repo structure: `src/` (ported detectors+portfolio),
   `services/loop.py`, `services/api.py`, `db/schema.sql` + `db/repo.py`, `docker-compose.yml`.
3. Wire live klines/DVOL fetchers (adapt existing `fetch_klines.py`/`fetch_dvol.py`).
4. Implement SELL-only execution against Bybit testnet options endpoints.
5. Mission Control 4th panel.

Per [[feedback_options_workflow_order]]: code review mandatory before test,
review again before any deploy — including the paper/testnet deploy itself.
