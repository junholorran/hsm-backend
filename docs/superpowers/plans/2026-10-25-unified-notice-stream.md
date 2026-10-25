# Unified notification stream

Complete the approved per-pair update format: radar liquidity and causal/FVG
notifications share the durable outbox and a notification envelope per M5
cycle. Distinct causal plans remain separate sections. Preserve source times,
no automatic causal relationship, no entry/fill inference, no trading changes.

- [ ] Freeze regressions matching the user's separate H1 liquidity/H4 FVG notices.
- [ ] Persist last-known liquidity per timeframe and currently tracked valid HTF POIs; seed old context without replaying old radar notifications.
- [ ] Route fresh live Ludo radar notices through existing outbox, remove the parallel live sender route, and keep raw context available for audit.
- [ ] Wrap same-cycle plan groups into per-pair pages; keep all original event receipts and page retry semantics.
- [ ] Verify stale/future data, restarts, old-policy suppression and independent opposite plans.
- [ ] Full suite/build/review, integrate and verify new deployment.

The daily complete map stays explicitly separate as a context report; these
changes unify the operational liquidity/FVG/plan update stream. Existing old
Telegram messages and already-frozen retry pages are not retroactively edited.
