# Clean daily bias presentation

User requested less information in the daily bias notification. Formatting only: keep nine lines normally, ten for an actionable daily-review reference. Show pair/as-of Portugal, existing daily hypothesis/reason, available MN/W1/D1/H4/H1 structural context, nearest available liquidity above/below in plain Portuguese, real operational stage and context-only footer. Do not invent absent macro data.

Remove last M5 price and H1/H4 POI lists from default compact message. Full map, all levels/zones and original evidence remain in detailed=True and daily_delivery_evidence. Daily calculation, stage/entry gates, source candles, daily UTC boundaries, reference levels, delivery_key, frequency, queues and frozen page receipts are unchanged. No release-only resend or presentation version bump. Pending old frozen pages retain original content; subsequent actual updates use new format.

Existing compact/report/delivery checks passed22 tests +23 subtests. One broad test asserted FVG inventory in the default message; update it to assert absence in compact and presence in detailed view. Full built suite408 +53 passed. No additional data calls or dependencies. Prior unresolved audit of old-capture reuse is not changed by this presentation task.
