# Tyagach — upgrade plan (2026-08-02 strategy redesign session)

Full evidence/numbers for every item: `RESEARCH_FINDINGS_2026-08-02.md`.
Current live state: 15m/OB, 30m/OB, 1h/OB deactivated; 2h/OB retuned
(r_target 3.0→5.0, expiry 1.0→0.25d) — deployed commit `cc34b58`. Everything
below is research-validated but NOT yet deployed.

## U1 — Rejection-close entry on the currently-live 2h/OB (lowest risk, ship first)

Pure entry-quality upgrade to a cell already in production — no new cell, no
capacity/combination question, no frequency change. `find_depth_entry` →
`find_rejection_entry` (require the touch bar's close to confirm the level,
not just wick through it).

- Validated: validation avg_pnl 1.95→7.82, holdout 6.59→8.11, win rate +5-9pp
  on both splits. Same config otherwise (depth=0.675, r_target=5.0,
  expiry=0.25d).
- **Action before shipping**: none needed beyond the standard
  architecture→code→review→tests→paper-deploy cycle — this is a direct,
  already-quarter-implied improvement (2h/OB's own quarter-robustness with
  rejection-close hasn't been run standalone, only spot-checked per-trade;
  run it once for the record before deploying, cheap).

## U2 — Decide 2h/OB vs 2h/FVG vs both (blocks on one more test)

2h/FVG+rejection alone is the strongest single candidate found all session
(8/8 quarters, mean +25.2%, worst +2.1%) — better than 2h/OB+rejection on
every axis tested so far. But the OB+FVG *combination* at 2h has never been
tested with rejection-close entries (the only combination check done used
the old touch-only entries, back when 2h/FVG's config was still the weaker
plain-touch one).

- **Action needed**: run 2h-OB(rejection) + 2h-FVG(rejection) combined
  portfolio + quarter-robustness (mirrors the U1/2h-FVG-solo checks already
  done). Only 1.8% zone overlap between OB and FVG at 2h (checked
  2026-08-02), so they're plausibly complementary rather than redundant —
  worth checking properly before picking.
- **Decision rule**: ship whichever of {2h/OB only, 2h/FVG only, both
  combined} wins on quarter-robustness (fewest negative quarters, best
  worst-quarter) without meaningfully worse drawdown than the solo winner.

## U3 — Solo per-TF validation for 1h/30m/15m × {OB, FVG} (before adding anything else)

The full grid re-sweep with rejection-close entries found robust per-trade
combos for **every** (kind, tf) pair — a huge change from before (only
2h/OB and, marginally, 30m/OB were alive pre-session). But per-trade robust
≠ portfolio robust ≠ quarter robust — proven twice this session (30m/OB,
and the multi-TF FVG combo). None of 1h/OB, 1h/FVG, 30m/OB, 30m/FVG,
15m/OB, 15m/FVG, 15m/BB(with rejection) have been checked SOLO at the
portfolio+quarter level yet — only per-trade (`results/
rejection_full_sweep_robust.csv`) and one spot-check (old 15m/OB config
flips positive, see RESEARCH_FINDINGS).

- **Action needed**: for each of the 6 untested (kind, tf) pairs, pick the
  best per-trade candidate from the robust CSV and run the same
  portfolio+quarter pipeline used for 2h/FVG. Cheap (~1-2 min per candidate
  based on today's runtimes) — this is mechanical, not open research.
- **Output**: a ranked list of which individual cells are actually
  deployable on their own merits (their own U1/U2-equivalent).

## U4 — Staged live rollout of validated cells (not a single big-bang deploy)

Once U3 produces a ranked list of solo-validated cells, add them to
ACTIVE_CELLS **one at a time**, not all together:

- Deploy cell N, let it accumulate a meaningful live sample (this project's
  own convention: 20-30 closes before trusting live vs backtest agreement)
  before adding cell N+1.
- Reason: the multi-TF FVG combination (U5 below) showed the *combined*
  portfolio behaves differently from the sum of solo cells (correlation
  during trend regimes even at low direct zone overlap) — staged rollout
  catches this on real data instead of trusting an uncalibrated capacity
  model.
- Each addition still goes through the full architecture→code→review→
  tests→paper-deploy cycle, same as every change this session.

## U5 — Multi-TF combination story: real but unresolved magnitude (do not deploy as one shot)

All 4 FVG TFs combined (rejection-close): 8/8 quarters positive at every
capacity-cap sensitivity level tested ($5k-uncapped), worst quarter always
+42.9% — directionally solid, the old correlation blow-up is fixed. But:

- Compounded return magnitude is not trustworthy at any cap tested (still
  "+2,800% over 4y" at the harshest $5k cap) — needs a real capacity model
  (actual Bybit ETH options depth/liquidity data), not built.
- Combination-selection methodology caveat: the 4 per-TF configs were each
  chosen as "best by train avg_pnl" among hundreds of robust candidates,
  then combined without a holdout-blind check on the COMBINATION itself.
  Selection was holdout-blind by construction, which mitigates but doesn't
  fully close this.
- **Do not deploy this as a single change.** Approach via U4's staged
  rollout instead — if U3's solo-validated cells get added one at a time
  and each performs well live, the combined story gets validated
  empirically rather than trusted from an uncapped backtest.

## U6 — Deferred / lower priority

- **Structure-invalidation exit** (opposite zone forms = thesis broken,
  exit regardless of price/time) — not implemented, needs a new code path
  since it depends on OTHER zones forming, not just this position's own
  price/time. Lower priority now that U1-U3's entry-filter gains are much
  larger than anything exit-mechanics found (partial-close and
  trail-to-breakeven were both dead ends this session).
- **Entry principle beyond rejection-close** (first-touch vs re-test
  distinction, volume/momentum confirmation) — not tried. Lower priority:
  rejection-close alone already produced the session's biggest win: revisit
  only if U1-U4 stop yielding further gains.
- **Real capacity/liquidity model for multi-TF sizing** — needed to make
  U5 deployable as a combination rather than only via staged rollout.
  Would need actual Bybit ETH options order-book depth data, not currently
  collected anywhere in this project.
- **Does rejection-close (or the broader haircut/portfolio-validation
  methodology) apply to Jony?** Deferred per user request 2026-08-02 —
  Jony is a different strategy (VRP straddle selling, not SMC zones), the
  literal entry mechanic may not transfer, but the underlying idea (don't
  count a level touch until price confirms) could have an analog worth
  checking once Tyagach's redesign settles.
- Pre-existing backlog (BB per-cell depth sweep, MB re-sweep, DOW effects,
  BTC version) — unchanged from `TYAGACH_IMPROVEMENT_PLAN.md`, orthogonal
  to this session's findings.

## Suggested order

U1 (ship now, lowest risk) → U2 (one test, then ship) → U3 (mechanical,
produces the U4 rollout list) → U4 (staged, takes weeks by design) → U5
(only after U4 gives live evidence) → U6 (opportunistic).
