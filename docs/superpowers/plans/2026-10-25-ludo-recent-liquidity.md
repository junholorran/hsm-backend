# Recent Ludo liquidity implementation plan

> Implement inline with test-driven development and review before integration.

Goal: connect confirmed recent H4/H1/M15 Ludo extremes to the existing capture/reaction/M15/POI/M5/retest pipeline, reproducing BNB 25 October without rewriting Lux50 or presuming fills.

Architecture: optional level history from the unchanged Ludo structural loop; a separate pure detector validates native pivot confirmation, availability, intactness and first closed-M15 crossing. The live provider feeds explicitly labelled Ludo captures into the existing durable chain. A persisted activation baseline prevents retrospective alerts on rollout.

Constraints: native closed contiguous candles only; strict wick crossing for capture; directional structural authorization still requires close; no automatic orders; unchanged Lux/FVG/OB calculations, reaction rules, structural invalidation, retest and late-observation protection. Do not implement continuation trades or notification consolidation here.

## Task 1: regression and detector
- [ ] Add frozen BNB native fixture and failing test for the 746.50 capture followed by 743.63 CHoCH and prospective short plan.
- [ ] Add tests for right-bar confirmation, gap/malformed data, already-crossed levels, no lookahead and symmetric lows.
- [ ] Expose optional Ludo level lifecycle without changing default structure output.
- [ ] Implement `kairos_ludo_liquidity.captures(tf_map, cutoff)` with explicit `LUDO_SWING_CAPTURE` origin.

## Task 2: durable live integration
- [ ] Feed new origins through `kairos_ludo_entry.snapshot` and `kairos_capture_chains.scan/advance`.
- [ ] Persist detector activation cutoff, reject pre-activation captures, preserve the cutoff across restart and retain existing plans/state version.
- [ ] Verify first post-activation capture survives restart and cannot create historical fills or bypass reaction/POI/refinement.

## Task 3: verification and delivery
- [ ] Run targeted tests, full repository suite and the production patch/build commands.
- [ ] Review the entire diff for detection coverage, lookahead, initialization, deduplication, runtime cost, and preserved execution safeguards.
- [ ] Commit and create a reviewable PR; integrate/deploy only after evidence supports it, respecting connector approval requirements.

## Review focus
- Same level republished with a later known timestamp must not resurrect consumed liquidity.
- Native pivot confirmation must occur before the capture candle, never during it.
- Gaps between level availability and capture must fail closed.
- Rollout/restart must not arm old retests or discard existing live plans.
- Fresh Ludo liquidity must not silently rename/redefine Lux50 or HTF FVG touches.
