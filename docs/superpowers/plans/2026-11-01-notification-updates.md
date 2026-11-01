# Kairos notification updates

Goal: shorten notifications without changing any market calculations or authorization.
Approved scope: immediate capture/FVG/structure/ready/retest/invalidation notices; daily map after native UTC D1 close; one status per closed H4 interval, shown in Lisbon time. Retain all source facts in durable outbox; no order execution.

- [x] Test concise capture/retest/cancellation, independent origins/directions, pagination and immutable inputs.
- [x] Implement presentation-only compact cycle formatter, preserving existing ready-plan formatter and all receipts.
- [x] Project all current plans and last four hours of actual events into daily report, read-only from existing state/outbox.
- [x] Test persistent four-hour cadence, new UTC days, retry/frozen payload/restart/old queued report behavior, unchanged source calculations.
- [x] Schedule daily/context delivery by UTC H4 bucket (not every stage change); render daily hypothesis, structure context, liquidity, zones, changes, current plans and next confirmation. Never label two-candle daily hypothesis as complete structural forecast.
- [x] Full patched tests, independent review, lease-protected publish, terminal deploy success and live logs.

Validation: production build patches + full suite461 passed,53 subtests; independent final review no blockers and28 focused tests. Only presentation, scheduler and read-only projection changed; original state-machine calculations/force/FVG/Lux extrema unchanged. Deploy verification recorded separately after publishing.
