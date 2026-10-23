# Ludo causal episode correction — 23 October 2026

The prior Ludo-only observer froze a capture's initially unresolved reaction and could associate arbitrary later structure with it. An invalidated plan did not consume its episode, and the latest dead origin could hide another active plan in the daily summary.

## Corrected contract

- Capture identity and first-confirmation clock stay immutable. Reaction is reconstructed from closed, contiguous M15 OHLC at each historical step using the existing two-adjacent-close classifier. No fixed expiry or indicator threshold is introduced.
- HIGH rejection/reclaim expects SHORT; HIGH acceptance expects LONG. LOW is symmetrical. An unresolved or contradictory reaction cannot qualify an actionable Ludo break. Reaction and structure may become known together at a close, never before it.
- Each actual confirmed reaction transition has a distinct key and can qualify its first aligned M15 break once. Invalidation cannot recycle that same episode. A new confirmed transition is explicit evidence, not a new liquidity capture.
- The causal interval starts at the first capture for its initial reaction, or at the previous reaction confirming bar for a later transition; original native Ludo pivot/zone rules further restrict the parent. This preserves the displacement extreme before the two confirming closes.
- HTF FVG interactions remain separate from liquidity captures, retain their native lifecycle, require matching native direction and one structural plan per touch. No Lux Major/Internal entry authorization is reintroduced.
- Migration rebases unqualified state at a validated cutoff. Old outbox rows remain for audit but unsent old-policy notifications are suppressed. Existing positions/orders are never touched. A restart on version2 preserves state/dedup.
- Missing first-capture M15 history fails closed rather than retaining a stale reaction. HTF M5-aligned touch retains its separate coverage rule. Active plans take presentation precedence over newer invalid origins.
- Messages include capture/touch confirmation and the confirmed reaction/time. Provider logs and output identify causal_policy_version=2.

## Evidence

Full suite with production flags: **285 passed, 53 subtests passed**. New regressions were observed failing against the old behavior before implementation. The native BTC fixture specifically fails against the original production module and passes with the correction. Independent review found the rolling-history stale-reaction edge case; its reproducer was added and fixed.

Same BTCUSDT Bybit public candles, 20 October17:15 →23 October17:15 Europe/Lisbon, 864/864 cycles, 24-hour initialization, no gaps:

| Metric | Before | After |
|---|---:|---:|
| Raw M15 sequence observations across origins | 73 | 19 |
| Raw M5 plans across origins | 32 | 16 |
| Distinct M5 plans grouped by break/zone/arming | 7 | 9 |
| Distinct prospective retest observations | 3 | 3 |

This is not a profitability comparison: fewer duplicate associations can coexist with more distinct eligible zones. The old short retest on22 October20:35 no longer qualifies from the reused H1 EQL82,344.70 or the old D1 PDL episode. The23 October04:20 long survives only under a different D1 low origin with a new reclaim confirmed at04:00; the H1 EQL82,344.70 cannot authorize it.

BNB event5aa35b27: D1 PDH749 captured23 October11:30; rejection confirmed11:45; first aligned SHORT Ludo CHoCH742.4 confirmed20:15. M5 IFVG746.14–748.45 was armed, with no retest observed through22:25. This is historical reproduction, not proof of an exchange fill or Telegram delivery.

All user-facing times above are Europe/Lisbon. Replay uses closed candles and arms after the observation cutoff; already-open M5 candles cannot become retrospective live retests. State migration and history guards added during review affect invalid/missing-history inputs; the three-day replay has complete capture history and uses no persisted pre-upgrade state.

No production FVG/IFVG/OB geometry, Ludo level computation, macro levels, daily bias policy, price feed, exchange account, orders or financial management was changed. Technical regression checks do not establish a profitable strategy or eliminate trading risk.
