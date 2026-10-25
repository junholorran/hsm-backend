# One operational update stream per pair

The earlier layout grouped causal plan proofs, but live radar capture notices
still used the separate app sender. This correction routes fresh Ludo radar
notices into the existing durable capture-chain outbox and returns an empty
legacy radar sender list. Raw `radar_context_events` remain available for audit.

`chain_notice_context` persists the last confirmation per liquidity timeframe
and valid, tracked HTF FVG touches. On rollout, old references seed the context
without replaying old radar notices. Subsequent FVG updates therefore retain
the known liquidity reference and its own confirmation time, even when they
occur in different cycles. The complete daily map remains a separate context
report rather than an operational entry message.

An envelope combines same-cycle presentation groups per pair. Distinct causal
origins, LONG/SHORT plans, zones, breaks and retests remain separate sections;
context references are not asserted to form one causal chain. Source event
acknowledgements and frozen page retry receipts are preserved. Radar durable
keys include the pair to prevent cross-pair collisions. Existing frozen pages
and previous Telegram messages are not retroactively rewritten.

No detector, reaction, structural gate, execution rule, order or policy-version
change is included. Validation: 328 tests and 53 subtests passed, including
9 new stream/context regressions; production build patches and post-build
suite passed. Independent review identified a cross-pair key collision; its
reproducing test failed before the pair-qualified key fix and now passes.
