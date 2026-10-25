# Organized Kairos alerts

Goal: apply the approved professional layout to live alerts, consolidate duplicate proofs for the same plan, and explain structural-reference side and invalidation without changing trading decisions.

Approach: retain new notifications on meaningful changes, rather than silently editing a single Telegram message. A pure presentation module groups only equivalent same-cycle/same-direction/same-break plans. Liquidity captures and HTF FVG touches stay explicitly distinct within the message. Existing context-map pages remain complete and clearly labelled as context.

Tasks:
- [ ] Add failing tests for grouped BNB proofs, opposite/different plans, invalidation explanation, buy/sell structural reference, HTML escaping and Telegram page limits.
- [ ] Enrich immutable alert facts with the existing plan snapshot and precise already-triggered invalidation reasons; do not alter conditions, timing or policy version.
- [ ] Render capture/HTF touch/M15 confirmation/M5 waiting/retest/invalidation/history stages in Portuguese with explicit next action and no presumed fill or fabricated stop margin.
- [ ] Freeze grouped delivery pages and receipts in SQLite, preserving original outbox events, retrying only unacknowledged pages, and acknowledging source events only after all pages succeed.
- [ ] Apply consistent labels to radar, HTF FVG and context notices without changing selection/deduplication.
- [ ] Run full tests before/after production patches, get independent review, then integrate and verify deployment/scanner.

Constraints: no signal/detector/math changes; no order placement/cancellation; no inferred execution or profitability; no mixed LONG/SHORT, timeframes as triggers, or unrelated POIs combined into one causal proof. Existing state/outbox survives rollout. Exactly-once Telegram delivery across a process crash is not claimed.
