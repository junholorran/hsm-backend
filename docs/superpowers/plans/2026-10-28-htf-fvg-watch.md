# H4/H1 FVG touch -> M15 observation

User request 28Oct: a bullish/bearish H4/H1 FVG touch must put the zone on watch for structural confirmation.

Implementation: separate context-only watcher; keep liquidity origins, M15 entry policy, Lux period levels, Ludo detector and daily context unchanged. Find the latest observed entry into each still-valid Ludo H4/H1 FVG from closed M5 bars, then its first later same-direction strong closed M15 event. Preserve the existing force gate. Aggregate overlapping origins for one M15 event. A known full M5 traversal invalidates the watch before the higher-timeframe bar closes. Reject data gaps and future data. Touch notices and structure notices use independent persistent delivery identities. Structure confirmation time drives boot-baseline suppression. Never reuse this as capture or entry authority. If the causal plan chain already reports that same M15 confirmation, suppress the redundant context confirmation.

Tests first: missing watcher module fails in isolated GitHub Actions; then implement helper, provider export and sender routing; run full production build suite and review. Test bullish/bearish, weak/open/past break, full mitigation, overlapping H4/H1, gaps, context-only message and unchanged input/dedup identity. Publish only the product files and tests after successful checks; keep audit workflow on isolated branch.
