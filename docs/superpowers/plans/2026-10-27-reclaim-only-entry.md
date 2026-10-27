# Reclaim-only liquidity entry correction

Goal: implement the user's explicit instruction to stop authorizing LONG after HIGH acceptance, and SHORT after LOW acceptance.

Scope: shared liquidity entry gate for complete M15 plans and early M5 notices. Require REJECTION_RECLAIM with HIGH→SHORT or LOW→LONG. Preserve acceptance classification/radar as context. Existing separately authorized HTF-FVG touch model and native detectors are unchanged; no guarantee about profitability or absolute tops/bottoms.

- [x] Add failing symmetric entry tests and real SOL 26 October reproduction.
- [x] Add shared gate; invalidate incompatible persisted plans/early hypotheses once at the next closed cutoff; retain capture history and valid rejection plans.
- [x] Suppress incompatible pending actionable events and frozen batches, retaining audit rows/receipts. Do not send stale acceptance plans after restart. In a mixed frozen batch, suppress the remaining batch conservatively rather than duplicate already delivered pages.
- [x] Verify state view cannot advertise incompatible plans even before the next scan. Test radar context remains, restart, rejection transition and valid opposite examples.
- [x] Run full suite/build checks, independent review, publish reviewed change and verify production status/logs.

No zone-overlap, SL/TP, indicators, native candle geometry or automatic account actions in this change.

Validation: first six regressions failed against the previous code; 13 new tests now pass. Full suite: 360 passed, 53 subtests. Production build patches passed, followed by 358 tests/53 subtests before the two extra review coverage cases. Real SOL fixture blocks the 26 October 12:45 LONG and permits later SHORT rejection structure. Native BNB M5-before-M15 and symmetric rejection tests remain passing. Partial mixed frozen batches retain their acknowledged-page count and are suppressed conservatively; subsequent valid batches deliver once. Independent reviewer found no blocking defect. Production publication/verification to follow.
