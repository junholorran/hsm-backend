# ICT FVG plan invalidation implementation plan

Goal: distinguish Ludo box mitigation from the closed-candle failure of an already armed FVG entry plan, with unambiguous notices.
Architecture: retain native Ludo structure, liquidity, zone formation and map lifecycle. Add a plan-only lifecycle check for saved Ludo FVGs, based on exact native ABC evidence and strict close beyond the far boundary. Other POI types retain their existing rules. Existing structural protection and opposite-break cancellation remain unchanged; this change does not claim that every existing rule is an ICT rule.
Reference: ICT 2024 Lecture 28, https://www.youtube.com/watch?v=HhgWGduQZQY&t=1886 (31:26–32:11), transcript at info.quagmyre.com. The lecture demonstrates bearish body-close failure, while allowing wicks. Bullish behavior is the mirrored implementation.
Constraints: no FVG geometry edits, no strength/entry/structure/capture edits, no automatic exchange orders, no revival of previously invalidated plans, no historical alert backfill, no stop widening. Apply only to validated native Ludo FVG plans. Close equal to boundary is not beyond. Missing ABC evidence fails closed with an explicit reason.
Review focus: malformed or missing evidence; history-evicted boxes; mirrored buy/sell; no retroactive fill claims; historical plans stay closed.

## Task 1: plan lifecycle and explanatory notifications
- [x] Write tests for wick-only penetration, strict close-through, exact equality, history eviction, missing evidence, both directions and full armed-plan transition.
- [x] Observe RED, implement plan-only removal evidence in kairos_capture_chains.py, observe GREEN.
- [x] Include zone, original break time, close and boundary in invalidation notices; test matching plan identity.
- [x] Run full production-patched suite and independent review.
- [ ] Publish with leased GitHub head and verify Railway SUCCESS plus fresh scans.

Ruling: use M15 closes for a M15 FVG plan and M5 closes for a M5 FVG plan; never infer an exchange fill or stop execution. This is an explicit software interpretation of the sourced example, not a claim of a universal ICT rule.

## Verification and review

2026-11-04: 22 new lifecycle/notice regressions passed; production-patched full suite: 499 passed, 53 subtests passed (pytest --ignore=legacy-mute). Build patch outputs app.py/scalp_engine.py restored; not part of this commit.

Fresh whole-change review: four Important findings, all addressed in one RED→GREEN pass:
- Waiting legacy parents retain native lifecycle until arming (test_unarmed_legacy_parent_keeps_native_wick_invalidation).
- Unverifiable saved ABC cancels before catch-up retests, and later bad data cannot mask an earlier proven breach (test_missing_abc_cannot_emit_positive_retest_during_catchup; test_proven_price_failure_is_not_masked_by_later_bad_data).
- Touch-zone geometry and its agreement with saved M15 parent are validated before overlap reads (test_bad_touch_zone_fails_closed_instead_of_throwing_or_touching).
- M5 failure notices include the affected M5 range even with a parent present (test_m5_failure_notice_identifies_actual_m5_range_with_parent_present).

No review findings deferred. No state/policy reset or historical resend. Deployment verified separately after publication.
