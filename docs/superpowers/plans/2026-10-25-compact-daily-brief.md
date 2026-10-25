# Compact daily briefing implementation plan

Goal: replace unsolicited full map pages with one compact Portuguese daily context summary.
Architecture: change only notification formatting; preserve the complete report in SQLite alongside frozen delivery pages. Keep original delivery identity, source calculations and causal authorization.

Design: default summary contains observation time, daily hypothesis and reason, separate structural D1/H4/H1 context, latest closed M5 reference, exact daily review condition, nearest available liquidity above/below, at most one closest valid POI per H1/H4, actual motor stage and manual DEMO context notice. A historical detector BOS is omitted from the summary. Full inventory remains available through notification_parts(..., detailed=True) and archived report evidence. No daily-rule or trading-gate changes.

Approaches: retaining all inventory with headings leaves the original overload; deleting map evidence loses auditability. Use compact delivery plus full persisted evidence. No new command or Telegram button is promised.

- [x] Reproduce overload, ambiguous review and historical BOS prominence with regression tests.
- [x] Implement compact default formatter and explicit detailed audit mode in kairos_daily_report.py.
- [x] Persist complete immutable report in daily_delivery_evidence, atomically with initial payload. Keep existing delivery version so completed messages are not replayed for formatting.
- [x] Adapt existing full-map and multipart-retry tests to explicit detailed mode, retaining their assertions.
- [x] Independent review, complete suite and production build script checks. Review found missing reaction/reset labels; reproducing test fixed. Final suite: 334 passed + 53 subtests. Build scripts and post-build suite passed before the final two-label copy fix.
- [ ] Publish reviewed tree and verify deployment tests, scanner and health.

Review focus: no invented entry authority; neutral/unknown data remains explicit; exact reference for each review reason; full evidence tied to initial frozen pages; old partial pages resume unchanged; no delivery identity bump or retrospective resend.
