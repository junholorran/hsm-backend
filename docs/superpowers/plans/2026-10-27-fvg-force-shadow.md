# FVG Force Shadow Implementation Plan

> **For agentic workers:** implement inline using superpowers:executing-plans.

**Goal:** Measure FVG impulse strength without changing entry decisions.
**Architecture:** Pure diagnostic module; opt-in provider result field, isolated errors; offline candle comparison.
**Tech Stack:** Python stdlib, pytest.
**Spec:** docs/superpowers/specs/2026-10-27-fvg-force-shadow-design.md

## Constraints and review focus
No lookahead, cross-timeframe association, changed Telegram/state, invented ICT thresholds or implied liquidity. Missing/zero history stays unknown. Mirror long/short. Third candle can be opposite color.

- [x] Write failing tests for causal geometry, metrics, mirrored cases, missing/gapped history, immutable inputs and observer isolation.
- [x] Implement pure assess_fvg and optional view observation.
- [x] Verify targeted and full existing suite; compare enabled/disabled provider output except diagnostic field.
- [x] Run read-only comparisons on archived SOL and BNB; document exact bounded evidence and limitations.
- [x] Commit isolated work; no production activation.

## Verification and review
11 shadow tests, full built application suite391 passed +53 subtests. Build patches passed; generated app.py/scalp_engine.py restored. Independent reviewer identified missing separate M15-break metrics; two reproducing tests failed, implementation corrected, full suite green. Review minor about archive history addressed explicitly in results; complete A/B/C geometry added. No deferred findings.

Ruling: strict direct-B hypothesis stays observation only; earlier leg FVGs remain measured and are not removed from live selection. Multi-candle strength interpretation remains research, not an enabled filter. Cost if strict hypothesis were activated prematurely: valid multi-candle setups could be excluded.

## V2 follow-up

User asked for flexible, volatility-normalized observation rather than copying BNB. ATR14 prior-only ratios and alternative2of3 diagnostic implemented with three new failing-first tests, then14 targeted green and full built suite394 +53 subtests. No new ATR gate, no live activation, no multi-candle aggregate strength claim. Same research-only deployment boundary remains.

V2 review: no blockers; minor two-of-three and opposite-direction test coverage added. Final15 shadow tests and full source suite395 +53 passed.
