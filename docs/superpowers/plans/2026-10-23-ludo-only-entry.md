# Ludo-only live entry plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:executing-plans inline.

**Goal:** One active structural entry provider: supplied Ludo M15, then causal M5 refinement and prospective retest. No Lux Major/Internal entry authorization in the live Telegram scanner.

**Architecture:** Route the live scanner to the existing durable independent-chain provider before invoking any legacy decision/phase path. Existing Lux map math remains context/liquidity/zone formation. Legacy replay functions remain explicitly offline reference; they cannot deliver live setups. Preserve both distinct origins: liquidity capture and native HTF FVG interaction.

**Tech stack:** Existing Python/SQLite/Telegram.

**Spec:** User explicitly requests entry solely via supplied SMC Ludo, removing conflicting Major/Internal authorization, preserving other detectors and DEMO/manual execution.

## Constraints
- Existing four pairs, same validated closed candles, no new data fetches or orders.
- Original Ludo structure math unchanged; no new M5 directional gate on an already authorized M15 leg.
- FVG touch is never called a liquidity sweep. Extract existing touch discovery without changing its geometry/timing.
- Opt-in `KAIROS_ENTRY_STRUCTURE=LUDO`; missing/invalid provider state fails closed, never falls back to Lux.
- No old setup/entry notification or retry may leak after switching.

## Review focus
- FVG-origin plans must survive restart without becoming fake liquidity captures.
- Pending legacy snapshots must not authorize live entries in Ludo mode.
- Missing storage/native evidence must not silently fall back to old authorization.
- Daily stage and capture messages must name the actual Ludo model.
- Same-cutoff persisted state and first activation must not backdate an entry.

## Task 1: Provider and origin preservation
Files: `kairos_ludo_entry.py`, `kairos_capture_chains.py`, `test_ludo_only_entry.py`, `scalp_engine.py` touch discovery extraction.
Interface: `snapshot(pair,tf_map,cutoff,engine)` returns the scanner contract, with durable provider stage and empty legacy setup/signal fields. `htf_touch_locations(...)` preserves existing causal interaction discovery.
- [x] Write and run failing provider/origin tests.
- [x] Implement provider and shared touch discovery; run tests plus existing HTF and capture regressions.

## Task 2: Live routing and presentation
Files: `scalp_engine.py`, `kairos_telegram_ui.py`, `kairos_context_map.py`, tests.
- [x] Test that Ludo mode never calls legacy evaluator/phase tracker, even with cached old setups or provider failure.
- [x] Route before legacy evaluation; update capture/daily presentation and model metadata.
- [x] Run full suite, independent review, fix important findings through red-to-green regressions.
- [ ] Commit reviewed tree, enable production policy, deploy and verify four-pair model logs.

Validation: 266 tests and 53 subtests passed on release tree. Native SOL provider smoke reproduced SHORT awaiting M5 retest while legacy authorization functions were instrumented to fail if called. All four Railway build patches passed and preserved provider routing/delivery. Independent review identified post-commit radar-export error leakage; red-to-green regression and isolation fix passed. No deferred review blockers. Deployment is the final verification step.
