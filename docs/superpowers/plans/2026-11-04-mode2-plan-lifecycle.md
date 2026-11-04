# Mode 2 Plan Lifecycle Implementation Plan

> Execute inline with test-first verification and one fresh final review.

Goal: activate the user-selected contextual-warning lifecycle for armed primary Ludo FVG plans.
Architecture: one eligibility helper in capture_chains; keep the original armed plan when reaction changes, and downgrade opposite breaks to deduplicated warnings. Existing native price/data safeguards remain. Brief notices identify the maintained plan. No policy version reset.
Spec: docs/superpowers/specs/2026-11-04-mode2-plan-design.md

## Review focus
Same-bar warning plus price failure; repeated reaction key; multiple independent/opposite origins; unarmed and invalidated records; missing evidence; no entry mutation or retrospective resurrection.

## Task 1
- [x] Add mirrored RED tests for opposite-break and reaction warnings, deduplication, warning then FVG/protection cancellation, no resurrection and unarmed parity.
- [x] Implement scoped mode-2 lifecycle and concise maintained-plan warning; run GREEN tests.
- [x] Replay BTC/ETH/SOL/BNB on a shared historical candle cut, baseline versus actual new implementation. M15 primary only; optional M5 refinement excluded equally, no fills/TP/P&L presumed.
- [x] Run production-patched full suite, fresh independent review, and fix Important findings with RED→GREEN.
- [ ] Publish with leased branch head; verify Railway SUCCESS and fresh four-pair scans.

## Verification record

2026-11-04: 516 tests and 53 subtests passed with the production build patches. Generated app.py/scalp_engine.py restored before publication.

Four-pair primary-M15 replay: 04/11/2026 00:00–18:30 Lisbon, warmup from 02/11 17:30, 345 evaluated cutoffs per pair. Exact native candle snapshots from the Kairos read-only exporter; contiguous finite M5/M15 coverage through the shared cutoff verified. Optional M5 refinement excluded equally; no saved production state/outbox used; no execution or P&L presumed. Same IDs, zones, protection, break and ready times for all five plans.

| Pair | Plans mode1/mode2 | Retests mode1/mode2 | Cancelled mode1/mode2 |
| BTCUSD | 1/1 | 0/0 | 0/0 |
| ETHUSD | 1/1 | 1/1 | 1/0 |
| SOLUSD | 3/3 | 2/2 | 2/1 |
| BNBUSD | 0/0 | 0/0 | 0/0 |

SOL morning SOL-b65632 remains monitored in mode2; the midday short still terminates on its FVG/structural price failure. These are limited historical observations, not evidence of superior expected returns.

Fresh independent review findings fixed in one RED→GREEN pass:
- Suppress a superseded maintained warning when the same delivery batch contains that plan's later terminal; preserve both source receipts (SQLite delivery regression).
- Coalesce identical plan/bar warnings despite different prior retest metadata; aggregate context reasons without losing origins' receipt seqs.
- Record and explain the new reaction state/direction while preserving the original armed episode.
No findings deferred. No historical plan is revived; no policy version or delivery-watermark reset.
