# Normal Ludo M15 plans implementation plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:executing-plans. Execute inline as explicitly requested by the user.

**Goal:** Authorize normal confirmed Ludo M15 breaks with causal FVGs; use strength only for the fire badge.
**Architecture:** Separate structural validity from the existing strength classification. Carry immutable break_strong metadata from break to plan events; render historical strict-policy plans compatibly.
**Tech Stack:** Python, pytest, existing native candles and SQLite outbox.
**Spec:** User agreement: normal Ludo for buys/sells, strong variant receives fire; all other gates retained.

## Global constraints
- Preserve capture/reclaim ordering, direct A/B/C FVG, protected extreme, retest, dedup and manual execution.
- No historical rearming, state reset, capture expiry, new liquidity or target filters.
- Existing valid plans and queued strict-policy events retain their meaning.

## Review focus
- Mirror long/short weak breaks and protected-extreme failures.
- No FVG, wrong B candle, or unclosed C must remain blocked.
- Strength metadata must reach ready notices and optional refinements without granting permission.
- Restart and same-cutoff replay must not generate old trades.
- Legacy strict events may retain fire; new unspecified strength must not invent it.

## Task 1: Regression and minimal fix
- [x] Add failing normal-break transition and direct-zone tests for both sides; fire classification tests.
- [x] Run tests red before implementation.
- [x] Separate valid_break and strong_break in kairos_m15_primary.py; remove strength gate from direct_zone only.
- [x] Update kairos_capture_chains.py to gate on validity and persist strength.
- [x] Update brief, compact and detailed renderers to use strength, not primary-policy truthiness.
- [x] Run production-patched full suite and historical ETH audit. Confirm ETH reaction ordering is unchanged.
- [ ] Review diff and publish only tested files against leased current head; verify Railway SUCCESS and fresh scans.

Validation: 477 tests plus 53 subtests passed with production build patches. Independent review found no blocking issues; stale force wording, mirrored SHORT coverage and persisted V1 continuation coverage resolved. ETH 12:30 remains blocked because rejection only confirmed at 12:45; no backfill. Publication verification recorded in deployment history.
