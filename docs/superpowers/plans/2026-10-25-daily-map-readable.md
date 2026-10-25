# Readable daily map implementation plan

**Goal:** Put a concise Portuguese daily reading before the complete existing inventory.
**Architecture:** Presentation-only wrapper around the existing plain-text formatter. Keep daily rules, state, delivery keys, persistence and detectors unchanged.
**Tech stack:** Python, unittest/pytest, existing Telegram HTML escaping.

## Agreed scope / short design

User requested the daily map prepared now using the concise layout discussed in chat. Use one summary followed by clearly labelled detail pages, rather than losing the existing inventory or keeping the unreadable inventory first. Show daily evidence, D1/H4/H1, nearby directional zones, nearest liquidity and actual stage. Never infer causal linkage from the latest independent M15 observation. Never invent an entry, stop, target, reversal confirmation or live quote. Preparing this change does not deploy it.

## Task 1

- [x] Verify baseline suite and reuse existing isolated worktree on a new branch.
- [x] Add tests: summary precedes inventory, exact daily comparison, non-mutating formatting, current vs historical setup, absent POI, zone proximity, both-side neutral, pagination and retained inventory.
- [x] Observe RED, implement presentation wrapper in `kairos_daily_report.py`, observe GREEN.
- [x] Run complete suite and inspect a SOL example. Review the diff independently before integration.

## Review focus

- A recent unrelated M15 observation must not be called the capture's confirmation.
- An old or invalidated plan must not display an active entry zone.
- The most distant historical zones must not crowd the first page.
- Pagination must respect Telegram limits and retain every inventory row.
- No delivery version bump: existing acknowledgements must remain valid, avoiding forced resend storms.

## Completion evidence

Baseline: 285 passed, 53 subtests. Final: 294 passed, 53 subtests. Nine dedicated formatting tests pass. Independent review identified precision rounding, neutral one-sided display and frozen capture reaction labels; all three reproduced RED and fixed GREEN. No remaining review findings. `git diff --check` clean.

Ruling: Keep complete historical inventory on subsequent detail pages, rather than discard data. Neutral summaries show nearest zones above, below and containing reference price per H4/H1. No mathematical, chain, scheduling or delivery-state changes. Existing delivery VERSION is retained, so acknowledged maps are not forcibly resent. Deployment remains unchanged.
