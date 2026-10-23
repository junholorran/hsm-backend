# Ludo causal reaction episodes implementation plan

Goal: prevent a stale capture or invalidated plan from authorizing arbitrary later Ludo breaks.
Architecture: retain the existing closed-M15 two-consecutive-close reaction classifier, computed from each historical prefix. Ludo remains the only structural entry gate; native FVG/IFVG/OB geometry stays unchanged.
Spec: user-requested capture → reaction → M15 Ludo → causal POI → M5 → prospective retest; no legacy Major/Internal gate; no fixed age cutoff.

## Constraints and decisions
- A confirmed reaction transition creates a distinct auditable episode, not another liquidity capture. HIGH rejection=SHORT, acceptance=LONG; LOW rejection=LONG, acceptance=SHORT.
- One first aligned M15 break/plan per episode. Invalidation cannot recycle that episode. New confirmed reaction transition, new capture or new valid FVG interaction can start a separate sequence.
- Reaction and break may become known at the same close; no historical authorization before that close. Confirmed reaction alone cannot create a trade.
- Causal POI sources must belong to the episode leg, beginning no earlier than the prior reaction confirming bar (or first capture bar for the initial reaction), then clamped by the original Ludo pivot. Preserve native zone rules.
- HTF FVG touch stays distinct; requires native zone evidence and matching directional structure, one plan per touch.
- Rebase old unsafe production state at first post-upgrade cutoff; retain audit rows but suppress old pending alerts. No retrospective entries.

## Task 1: regression, reaction and consumption
- [x] Write failing behavior tests: unresolved reaction blocks; reaction direction mismatch blocks; reaction updates with closed bars; invalidated episode cannot rearm; new confirmed reaction can start new sequence; no lookahead in catch-up; episode POIs cannot use old reaction sources.
- [x] Implement reaction episode helper and integrate advance, preserving Ludo structure and zone lifecycle.
- [x] Run focused and full suites, updating old fixtures that intentionally asserted unlimited reuse.

## Task 2: durable migration and visible evidence
- [x] Write failing migration, queued-delivery suppression, active-summary selection and message provenance tests.
- [x] Implement versioned state rebase, delivery suppression and active-first summary; add capture/reaction timestamps to actionable observations.
- [x] Run full suite with production flags and real BTC fixture regression.

## Task 3: verification and release
- [x] Replay identical three-day BTC inputs before/after, inspect each new actionable chain and BNB example where data supports it.
- [x] Independent whole-branch review; address material findings with regression tests.
- [ ] Publish tested tree through GitHub, deploy and verify terminal Railway SUCCESS and all four pair logs.

Review focus: forming candles; same-close ordering; missing capture history; restart/pending unsafe alerts; multiple simultaneous origins hiding active plans.

## Execution ledger
- Existing isolated worktree verified; branch fix/ludo-causal-episodes. Baseline266+53 passed.
- Eight initial regression tests failed as intended; implemented reaction qualification, per-transition consumption and auditable timestamps.
- Ruling: a causal displacement begins before its two confirming closes. The interval boundary is the previous reaction confirming bar, further clamped by the native Ludo parent pivot; using only the latest two bars removed the legitimate SOL fixture POI. Covered by test_reaction_leg_keeps_the_extreme_before_the_two_confirming_closes.
- Migration/outbox regressions observed RED→GREEN; migration validates closed contiguous inputs and never moves cutoff backwards.
- Independent review found missing first capture history could retain stale direction. Reproduced RED; liquidity coverage now requires first_capture_ts; M5-aligned HTF touch keeps its separate confirmation coverage.
- No fixed age cutoff, scoring, Ludo structure changes, legacy Major/Internal gate or native FVG/OB lifecycle changes.

- Final: fixed missing first-capture history (P1) — test_missing_capture_bar_cannot_preserve_stale_wrong_side_reaction RED→GREEN; production-env suite285+53 green.
- Final: migration validation/backwards-cutoff regressions RED→GREEN; native BTC baseline-old fails and corrected policy passes.
- Three-day replay864/864, no gaps, each actionable break carries aligned historical reaction; no repeated M5 arming per capture/episode. BNB event5aa35b27 reproduced through22:25.
