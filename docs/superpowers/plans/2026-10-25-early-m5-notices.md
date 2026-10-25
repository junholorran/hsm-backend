# Early M5 structural notices

Goal: notify the first reaction-qualified Ludo M5 break without waiting for M15, retaining later M15 confirmations and the existing entry plan model.
Architecture: independent observer state persisted in chain_state; events use the existing durable outbox/unified formatter. No order authority, no claim of a complete entry from structure alone.

Design: closed native M5 bars, known swing before break, confirmed reaction available at the break (existing M15 reaction classifier or native HTF touch), matching direction and captured-level side. One M5 observation per origin/reaction episode. Record exact break close and observed time. Initial rollout baseline prevents retrospective alerts. Track reaction changes, contrary M5 closes, breached structural extreme and invalid HTF origin; ending an early observation does not consume/cancel the existing M15 episode. Do not reinterpret M5 as M15 or call an unverified POI/retest an entry.

Approaches: moving entry authorization to M5 changes the trading model and is outside this notification request. Reuse the separate native structure/reaction evidence for an early observation while preserving existing M15 plans and all native detector math.

- [x] RED regression: BNB 24 Oct M5 20:35 before M15 22:00; prefix only, no future evidence; rollout and restart dedup.
- [x] Implement kairos_early_m5.py observer, call within the chain scan transaction, no provider order/watermark changes.
- [x] Explicit M5 observation/invalidation presentation with original confirmation and source ID. Update waiting copy to reflect early observation plus later M15 authority.
- [x] Verify unresolved/wrong-side/future reaction, consumed episode, opposite structure/extreme/reaction/origin invalidation, delayed timestamps and later M15 preservation.
- [ ] Independent review, full suite/build, reviewed commit and production verification.

Validation: 13 new tests; full suite 347 passed + 53 subtests. Production build scripts passed, and post-build full suite passed after restoring the Ludo label in capture copy. Independent review reproduced an outside-bar structural breach on the very M5 break candle; test failed before the guard, then passed. Reviewer verified the fix and found no further material issues. M5 observations remain NOT entry-authorized; no new order or causal-plan math.
