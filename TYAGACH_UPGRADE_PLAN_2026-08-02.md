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

## U2 — Decide 2h/OB vs 2h/FVG vs both — **DONE, SHIPPED** (`9b795fa`)

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
- **Result** (`src/u2_ob_fvg_rejection_combo.py`): combined wins. 0/8
  quarters negative (tied with FVG-only, vs OB-only's 1/8), mean quarterly
  return +30.0% (vs FVG-only's +25.2%), worst quarter +1.2% (vs FVG-only's
  +2.1% — still solidly positive) drawdown deltas <1pp and actually lower
  than FVG-only on validation/holdout. Portfolio (60/20/20): holdout return
  OB-only +6.6% / FVG-only +27.7% / combined +37.3%.
- **Shipped**: FVG added as its own tradeable Zone kind end-to-end (was
  previously only used to widen OB zone boundaries) — `tyagach/core/zones.py`,
  `tyagach/services/signal_engine.py`, `tyagach/services/config.py`
  (CELL_CONFIG depth=0.675/r_target=10.0/expiry=0.167, PRIORITY=2 tied with
  OB, WEIGHT_PCT=0.12, MAX_OPEN_PER_ZONE=3 — same risk-budget tier as OB,
  matches what was actually backtested). 7 new tests
  (`tyagach/tests/test_fvg_zone_kind.py`), 86/86 total pass. Independent
  review confirmed live FVG→Zone mapping and rejection-close entry are
  byte-identical to the backtest (`src/fvg_depth_sweep.py`,
  `src/fvg_rejection_entry.py`). Deployed to VPS3, confirmed live via
  `config.ACTIVE_CELLS`/`cell_config("2h","FVG")` inside the running
  container, balance/positions untouched, no errors in loop logs.

## U3 — Solo per-TF validation for 1h/30m/15m × {OB, FVG} — **DONE**

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

**Result** (`src/u3_solo_validation.py`, each cell run SOLO — no capacity/
combination story yet, that's U4/U5):

| pair | neg quarters | mean %/q | worst %/q | worst maxDD | config (depth/r_target/expiry_d) |
|---|---|---|---|---|---|
| 30m/FVG | 0/8 | +53.5% | +16.2% | 6.8% | 0.300/7.0/0.125 |
| 1h/FVG  | 0/8 | +40.9% | +9.8%  | 7.1% | 0.325/7.0/0.167 |
| 15m/FVG | 0/8 | +61.1% | +9.7%  | 11.8%| 0.300/10.0/0.125 |
| 30m/OB  | 0/8 | +7.3%  | +0.3%  | 7.0% | 0.300/10.0/0.125 |
| 1h/OB   | 1/8 | +3.4%  | -0.8%  | 6.1% | 0.650/5.0/0.125 |
| 15m/OB  | 1/8 | +5.4%  | -5.8%  | 8.2% | 0.300/10.0/0.125 |

- **4 cells cleanly pass** (0/8 negative quarters): 30m/FVG, 1h/FVG,
  15m/FVG, 30m/OB. FVG is again the strongest signal source this session —
  all 3 FVG timeframes rank above every OB timeframe.
- **1h/OB marginal**: 1 negative quarter, but shallow (-0.8%) — low-risk if
  added.
- **15m/OB flagged, not auto-recommended**: 1 negative quarter AND the
  60/20/20 validation split alone is net negative (-8.1% return, calmar
  -0.78) even though train/holdout are positive and the quarter-level worst
  is only -5.8% — the same per-trade-robust-but-portfolio-fragile pattern
  that killed the OLD 15m/OB before this session's rejection-close fix (see
  U1 notes). Rejection-close clearly helps it (per RESEARCH_FINDINGS, it
  flips all 3 splits positive at a DIFFERENT config than the one picked
  here purely by train avg_pnl) but this specific best-by-train candidate
  still has a shakier validation split than the other 5. Re-pick a candidate
  favoring validation robustness before deploying, or deploy last / skip.
- **Compounded % returns on the `train` split are NOT deployment-relevant**
  (e.g. 15m/FVG train +1146%) — same caveat as every other solo-compounding
  number this session (see U5 below): realistic only once run through the
  same shared-account capacity constraints all other live cells face. The
  quarter-level numbers (fresh $2000 each) are the trustworthy figures here.
- **Reminder for U4**: per-trade robust != portfolio robust != quarter
  robust != combined-with-everything-else robust (proven 3x this session:
  old 30m/OB, multi-TF FVG, and now this table shows even solo-portfolio-
  robust isn't automatic — 4/6 pass cleanly, 2/6 don't). Each cell U4 adds
  still needs its own live sample before trusting it, even after this
  backtest pass.

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
