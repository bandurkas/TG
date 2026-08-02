# Research findings — 2026-08-02 (strategy redesign session)

> Running log, updated as findings land. Not yet all deployed — see status
> per item. Deployed items reference the commit. Everything here uses
> haircut=0.85 (realistic sell-fill, confirmed on 27 live closed trades)
> baked into the pricing from the start, unless noted.

## DEPLOYED (commit cc34b58)

- **15m/OB, 30m/OB, 1h/OB deactivated.** No robust (depth, r_target, expiry)
  combo anywhere in a wide grid (r_target 1.5-20) clears positive avg_pnl on
  all 3 splits under realistic haircut. 30m/OB passes per-trade but fails at
  portfolio level (drags combined book to -11%/-13% train/validation).
- **2h/OB retuned**: r_target 3.0→5.0, expiry_days 1.00→0.25 (depth_frac
  0.675 unchanged). Only cell that survives realistic friction from the
  original OB detector. Quarter-robustness: 5/8 positive, worst -1.5%.
  Portfolio: train +6.4%/val +0.5%/holdout +4.7%, maxDD ≤5.8%.

## NOT YET DEPLOYED — validated, ready to ship

- **Standalone FVG (imbalance) zones are a real, independent signal.**
  `structure.detect_fvg` already existed but was only ever used to widen OB
  zone boundaries — never traded on its own. Only 1.8% of OB zones have a
  nearby same-direction FVG (8/435 on 2h) — genuinely different setups, not
  duplicates. New detector: `src/fvg_depth_sweep.py` (`detect_fvg_zones`,
  reuses OB's exact entry/stop/pullback-depth machinery for apples-to-apples
  comparison).
- **2h/FVG (depth=0.675, r_target=10.0, expiry=0.167d **[4h, 2 bars]**)** —
  current best candidate found this session, beats 2h/OB on every metric:
  - Quarter-robustness: **7/8 positive** (vs 5/8 for 2h/OB), mean +13.5%
    (vs +1.4%), worst quarter -0.1% (vs -1.5%), maxDD ≤8.4%/quarter.
  - Portfolio: train +134.3%/val +7.8%/holdout +6.2%, on ~1000 train trades
    (vs 129 for 2h/OB — 8x the sample, much less noisy signal).
  - **Exit-timing finding**: the OB sweep's expiry grid floor was 0.25d
    (6h/3 bars) — never tested shorter. FVG's much larger sample (8x OB's)
    made a clean signal visible: 2 bars (4h) beats both 1 bar (2h, too fast,
    win rate craters to ~0.6) and 3 bars (6h, the old floor) on ALL THREE
    splits, for every (depth, r_target) combo tested. Re-checked the same
    2-bars-vs-3-bars question for 2h/OB: **no clean winner** — splits
    disagree (train prefers 2 bars, validation prefers 6 bars, holdout
    prefers 3 bars) because OB's sample is 8x smaller and noisier. **Do not
    apply the 2-bar exit finding to OB** — only validated for FVG.
  - Scripts: `src/fvg_portfolio_compare.py` (patches `tyagach_samedir_ab`'s
    module-level `WEIGHT_PCT`/`MAX_OPEN_PER_ZONE`/`PRIORITY` to add an "FVG"
    key at OB's tier — gotcha: patching a *local* copy of those dicts does
    NOT reach `simulate_tagged`, which reads its own module's globals; must
    patch `tyagach_samedir_ab.WEIGHT_PCT` etc. directly or every FVG
    candidate silently gets a $0 budget and never opens).
  - Open decision: **combine 2h/OB + 2h/FVG, or replace?** Combined portfolio
    test: train +138.1%/val +1.2%/holdout +7.6%, but validation maxDD jumps
    to 15.3% (vs 4.5% for OB alone / 8.3% for FVG alone at its own best
    expiry) — combining does NOT cleanly diversify despite low zone overlap;
    likely both are still directional bets on the same underlying that
    correlate during trend/vol regimes. Not a free lunch. Leaning toward
    2h/FVG as primary (best solo risk-adjusted profile found), OB kept as
    secondary or dropped — undecided, need one more comparison round.

## Confirmed dead ends (do not repeat without new data)

- **Multi-TF FVG combos blow up.** 2h-FVG + 1h-FVG: validation -18.2%
  (maxDD 27.8%), holdout -4.1%. Adding 30m-FVG too: validation -49.2%
  (maxDD 50.7%). Same cross-TF correlation trap as the 07-05 MB incident
  (three same-direction positions on different TFs hit SL together) —
  `per_tf` same-direction scope isn't enough protection when multiple TFs on
  the same underlying correlate in a trend. 1h/FVG and 30m/FVG pass their
  OWN per-trade AND per-trade-portfolio-solo screens (see below) but fail
  the moment they're combined with 2h — another instance of the per-trade
  vs portfolio-combination pitfall, one level up from the single-cell
  version that killed 30m/OB.
- **15m fails under every zone type tested (OB, FVG).** Not an OB-specific
  problem — likely the timeframe itself (fee/haircut drag too high relative
  to premium size at 15m's cadence, or theta capture window too short to
  matter net of the ~85% haircut). Do not retry 15m with a new zone type
  without addressing the underlying frequency/friction ratio first.

## Confirmed dead ends, round 2 (exit mechanics, 2h/FVG @ depth=0.675/expiry=4h)

- **Partial profit-taking (close half at a nearer r_target, let the rest
  ride to r_target=10) is strictly worse.** Tested rt_near in {2,3,4,5} vs
  the single-leg r_target=10 baseline, all 3 splits: avg_pnl always lower
  (e.g. holdout 2.64->2.20 at rt_near=2), converging toward baseline as
  rt_near->10 but never beating it. Checked risk-adjustment too, not just
  EV: std barely drops (holdout 18.48->18.00, ~3%) while mean drops more
  (~17%) -- the risk-adjusted ratio gets WORSE, not better. Worst-case trade
  is IDENTICAL to the single-leg version (-151.44 both) because both legs
  share the same SL -- partial-close alone does nothing about tail risk,
  which comes entirely from the SL side. Script: `src/fvg_partial_close.py`.
- **Trailing the stop to breakeven after a partial-profit trigger is a
  no-op at the current best exit timing.** This was meant to fix what plain
  partial-close couldn't (tail risk) by tightening the stop once price
  moves favorably. At expiry=4h (2 bars), delta vs single-leg was EXACTLY
  0.0000 across every rt_trigger tested and every split -- verified this
  wasn't a bug by re-running with expiry=1 day (12 bars), where the same
  code correctly shows 28/377 trades diverging. **Root cause: 2 bars is not
  enough time for "trigger fires, then price reverses far enough to
  matter" to happen** -- whatever bar the trigger touches in, there's only
  one bar left before expiry, and empirically the close-price exit at
  expiry lands the same regardless of which stop level was active. The
  very thing that makes the 4h exit good (minimal market exposure) is what
  makes trailing-stop mechanics irrelevant -- these two ideas are in
  tension, not complementary. Do not revisit trailing stops without also
  loosening the expiry (which independently tested worse on raw EV -- see
  exit-timing finding above). Script: `src/fvg_trail_to_breakeven.py`.

## NOT YET DEPLOYED — validated, strongest candidate found this session

- **Rejection-close entry filter for 2h/FVG.** Current `find_depth_entry`
  triggers the instant price wicks through `entry_level`, regardless of
  where that bar closes -- a wick-and-continue (fakeout) counts the same as
  a genuine rejection. New `find_rejection_entry` (`src/fvg_rejection_entry.py`)
  only accepts the touch if the SAME bar's close is back on the favorable
  side of `entry_level`; otherwise keeps scanning (not invalidated, same as
  any non-touching bar). ~10-12% fewer entries (filters out the fakeouts)
  but massively better quality:
  - Per-trade avg_pnl: train 3.81->5.02, validation 2.11->5.57 (+164%!),
    holdout 2.64->4.67 (+77%). Win rate up ~7pp on every split too.
  - Portfolio: validation +7.8%->+38.7% (maxDD 8.3%->3.2%, LOWER risk
    despite much higher return), holdout +6.2%->+27.7% (maxDD ~same).
    Calmar: validation 0.94->12.07, holdout 1.06->5.08.
  - **Quarter-robustness: 8/8 quarters positive** (up from 7/8), mean
    +25.2% (up from +13.5%), worst quarter +2.1% -- not even negative.
    Every quarter's maxDD under 8%.
  - This is the strongest, cleanest result of the whole session. Same
    (depth=0.675, r_target=10.0, expiry=0.167d/4h) as the plain-touch 2h/FVG
    config, just the entry trigger changed.
  - **Consistency check DONE, and it's a bigger deal than expected**:
    rejection-close also massively improves 2h/OB (validation avg_pnl
    1.95->7.82, holdout 6.59->8.11) -- this is a GENERAL entry-quality fix,
    not FVG-specific. More importantly: tested it against the OLD deployed
    15m/OB config (depth=0.575/r_target=10.0/expiry=0.25d, the one
    conclusively killed earlier this session) and it **flips all 3 splits
    from negative to positive**: train -0.20->+1.07, validation
    -1.64->+0.41, holdout -0.12->+1.54.

## ⚠️ SUPERSEDED FINDING — re-sweep in progress

- The earlier claim "15m fails under every zone type tested (OB, FVG), not
  an OB-specific problem" (see Confirmed dead ends above) was true only for
  the plain wick-touch entry. Rejection-close entry flips 15m/OB positive
  on all 3 splits (see above). **Do not trust the earlier 15m/30m/1h
  deactivation conclusion or the OB/FVG depth-r_target-expiry grids until
  they're re-run with find_rejection_entry instead of find_depth_entry** --
  every number in results/ob_depth_sweep_haircut85_*.csv and
  results/fvg_depth_sweep_haircut85_*.csv used the old touch-only entry and
  may be understating what's actually achievable on 15m/30m/1h. Full
  re-sweep across both zone types x all 4 TFs in progress.

## NOT YET DEPLOYED — rejection-close entry is a general fix, changes everything above

- **Full re-sweep done** (`src/rejection_full_sweep.py`): rejection-close
  entry applied to BOTH zone types x all 4 TFs. Result: **every single
  (kind, tf) combination now has robust per-trade combos** -- 3557/4896
  vs 265/4080 (FVG touch-only) / 165/3264 (OB touch-only). This is a much
  bigger lever than the zone-type question. Full results:
  `results/rejection_full_sweep_{full,robust}.csv`.
- **Multi-TF FVG combination no longer blows up** (contradicts the earlier
  "Confirmed dead ends" entry below, WHICH USED THE OLD TOUCH-ONLY ENTRY):
  combining all 4 TFs of FVG (rejection-close, best per-TF picks) gives
  **8/8 quarters positive**, worst quarter +42.9%.
  - **BUT the magnitude is not yet trustworthy and needs a sober re-check
    before acting on it**: validation +314.4%, holdout +510.4%, one
    quarter +1869.1%, at ~15-20 trades/day sustained. This reeks of a
    compounding artifact (this project's own docs already flag: the
    backtest uses a fixed HALF_SPREAD and does not model liquidity
    degradation as position count/size grows -- unrealistic at this
    trade frequency and implied position sizing on a real, finite ETH
    options market). Also: the `per_tf` same-direction block only stops
    stacking WITHIN one TF, not across TFs firing simultaneously in the
    same direction on the same underlying -- the exact thing that broke
    the OLD multi-TF MB combo on 07-05. Whether rejection-close entries
    also fixed THAT specific correlation risk (not just the aggregate
    return number) is unverified. **Do not deploy any multi-TF combo
    based on these compounded % numbers alone.**
  - **Sober re-check DONE (fixed $1000 notional/trade, no compounding)**:
    all 4 FVG TFs combined, rejection-close entries, full 4y history:
    29,762 trades, total $30,921 profit, ~$21.2/day average. Per-TF avg
    pnl/trade: 2h $2.03 (n=1577), 1h $1.49 (n=3603), 30m $1.07 (n=7673),
    15m $0.83 (n=16909) -- shrinks as TF gets faster (fee/haircut drag
    bites proportionally harder on smaller premiums) but stays positive at
    every TF. **Conclusion: the underlying combined edge is real, not a
    mirage** -- but it's modest and steady, not the 300-500%
    compounded-return story. True achievable return sits somewhere between
    this fixed-size floor and the naive-compounding ceiling; realistic
    assessment needs a capacity-aware sizing model (cap position growth or
    model real margin/liquidity limits at ~20 trades/day combined), not
    built yet. **Do not deploy the "all 4 TFs" story on the compounded
    numbers -- the fixed-notional evidence supports it directionally, but
    execution realism at that frequency needs checking first.**

## ⚠️ Also superseded (used the old touch-only entry)

- The "Multi-TF FVG combos blow up" finding further below (validation
  -18.2%/-49.2% for 2h+1h-FVG / 2h+1h+30m-FVG) used plain touch entries.
  Not yet re-tested whether it holds with rejection-close -- the one
  multi-TF rejection-close test done so far (all 4 FVG TFs together) did
  NOT blow up, so this finding may not survive either. Re-check before
  trusting either version.

## Open question from user (2026-08-02, deferred)

- **Does the rejection-close entry finding (and the broader haircut/
  portfolio-validation methodology from this session) apply to Jony too?**
  Not investigated yet -- Jony's engine (`~/Desktop/Jony/research/
  jony_engine.py`) is a different codebase/strategy (VRP-based straddle
  selling, not SMC zones), so "rejection-close" as literally implemented
  here may not transfer directly, but the underlying idea (don't count a
  level touch until price confirms/rejects there) could have an analog.
  Revisit after Tyagach's redesign is further along.

## Not yet tested — next round

- **1h/FVG and 30m/FVG solo** (not combined with 2h) — per-trade robust
  (see fvg_depth_sweep_haircut85_robust.csv), never quarter-tested alone.
  Given the multi-TF combination trap above, if either is used it should
  likely REPLACE 2h rather than run alongside it, or the whole multi-TF
  correlation problem needs a real fix (e.g. a cross-TF same-direction
  block, which tyagach_samedir_ab.py's own history already flagged as an
  open question back on 07-05 and never resolved).
- **Entry principle beyond depth_frac retracement**: currently a static
  fraction of zone height. Not yet tried: requiring a rejection
  candle/wick at the entry level before counting a touch as valid, or
  distinguishing first-touch vs re-test entries.
- **Structure-invalidation exit** (opposite zone forms = thesis broken, exit
  regardless of price/time) — not yet tried, would need a genuinely
  different code path (exit condition depends on OTHER zones forming, not
  just this position's own price/time).

## Methodology reminders (validated the hard way, twice, this session)

- **Always check portfolio-level (compounding, real caps, real fees) after
  any per-trade-only sweep result** — burned by 30m/OB (per-trade robust,
  portfolio-negative) and by the multi-TF FVG combos (each TF solo-robust,
  combination-negative). Per-trade avg_pnl>0 on 3 splits is necessary, not
  sufficient.
- **Sample size matters for how much to trust a sweep dimension.** FVG's
  8x-larger sample let a clean 2-bar-exit signal emerge that OB's smaller
  sample couldn't confirm (split disagreement = noise, not signal). Don't
  transplant a fine-grained finding from a high-n zone type to a low-n one
  without re-checking.
