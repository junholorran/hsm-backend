# Recent Ludo extremes in the live causal chain

## Problem and behavior

The live Ludo structure observed BNB's recent 746.50 high and the bearish
743.63 M15 CHoCH at 2026-10-25 06:45 UTC, but the capture registry only supplied
Lux50/equal/previous-period liquidity and HTF FVG interactions. It did not
supply that recent confirmed Ludo extreme as a capture origin.

`kairos_ludo_liquidity.captures` adds explicitly labelled H4/H1/M15 Ludo swing
captures. It reuses optional level lifecycle evidence from the existing Ludo
loop, requires a closed native right-hand candle confirming the local turn,
verifies full native/M15 coverage and freezes the first strict M15 crossing.
Equal touches and levels crossed before their confirmation are ineligible.
Lux50, FVG/IFVG/OB, reaction classification and structural math are unchanged.

These are additional origins, not automatic trade signals: the existing
reaction, first aligned M15 break, causal POI, M5 refinement, prospective
retest and invalidation gates still apply. Continuation entries and unified
notifications are outside this correction.

## Rollout

The durable state stores `ludo_liquidity_since` on first evaluation. Captures
at/before that cutoff cannot be newly adopted by this detector. This baseline
survives restart; existing v2 plans and their watermarks are preserved. No
historical fill or order is created. Future notifications use the existing
capture-chain outbox and label the level `LUDO_SWING_HIGH/LOW`.

## Evidence

- Frozen Bybit native BNB fixture reproduces 746.50 first crossing in M15
  04:00–04:15 UTC, then first aligned bearish CHoCH at 06:45 UTC.
- Reproduced M15 OB 747.38–748.63 and M5 refinement 747.47–748.63.
- No prospective retest was observed in the tested window; no fill is claimed.
- Baseline suite: 285 tests and 53 subtests passed.
- Proposed suite: 297 tests and 53 subtests passed, including 12 new regressions.
- Production patch/build scripts completed; post-build suite also 297/53 passed.
- Independent review found no actionable critical/important issue; historical
  prefix consistency was checked across 24h of fixture M15 cutoffs.

This is an integration regression fix, not proof of universal ICT parity,
profitability, or zero defects on every market/history window.
