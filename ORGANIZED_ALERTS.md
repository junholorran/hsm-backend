# Organized live alerts

The approved layout identifies the pair, sequence, observation time in
Portugal, individual liquidity/POI origins, structural confirmation, M15/M5
zones, planned proximal, structural-reference side, current state and next step.
The structural extreme is not represented as an exact stop or computed margin.
Retest observations never claim an exchange fill. Invalidations identify the
existing triggering facts and confirmation time, and do not cancel orders.

Equivalent same-cycle M15 confirmation and M5 readiness proofs share a notice.
Origins are individually labelled; FVG touches do not imply liquidity capture.
Opposite directions, different zone identities, different break/candle times,
different invalidation facts and different observation cycles remain separate.
The context map remains separate and explicitly does not authorize entries.

The original outbox is preserved. Frozen grouped pages are persisted in
`chain_delivery_batches`; source events are acknowledged only after every
page succeeds. Retries resume after the last acknowledged page. Later queued
events do not change frozen notices; delayed retries receive a historical
warning. Batches from a superseded causal policy are suppressed. Delivery
retains the existing single-process submit worker. Exactly-once delivery
across Telegram acceptance followed by process failure is not promised.

No detector, entry, reaction, invalidation condition, state policy version,
order execution or position-management rule changes in this presentation fix.

Validation: 319 tests and 53 subtests passed on the build-patched source,
including 22 presentation/delivery regressions. Production patch scripts pass.
Independent review identified zone-identity grouping, distinct retest times,
and missing reaction-invalidation time; each has a reproducing regression and
was corrected. Legacy queued proofs retain their structural confirmation.
