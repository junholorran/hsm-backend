# Unified Kairos context map

One presentation in the existing four-pair Telegram bot. No new exchange calls,
orders, indicator thresholds, entry authorization or detector changes.

## Daily reading
Compare the last two consecutive closed native UTC D1 candles. A close above the
previous high gives a bullish hypothesis; below its low, bearish. A high-only
capture and inside close gives bearish; low-only capture and inside close,
bullish. Both sides captured, inside bars and exact boundary closes are neutral.
Both-side capture has priority even when the close is outside. This is an explicit
context heuristic, not an empirically established forecast or full ICT method.
The next native D1 close replaces the hypothesis. Intraday crossing of the
relevant prior-day boundary adds a review notice, not an automatic reversal,
stop or entry instruction. Candle color alone is never the rule.

## Existing map
An optional export carries H4/H1 FVG/IFVG/OB states from the map already built by
`avaliar_vortex_decision_layer_v2`. The daily hook consumes its existing structural
registry and does not fetch additional candles. A fallback registry calculation
exists for standalone tests/callers, but production always supplies the registry.
Only available, noninvalidated, geometrically valid zones are displayed. Zone
availability is formation/break/flip candle close on its native timeframe.

The reference price is the last closed M5 close, explicitly not a live quote.
Nearest available structural liquidity is listed on either side. Existing closed
M15 capture evidence overrides native ACTIVE *in the presentation only*, preserving
native state/provenance; this avoids advertising already consumed H1/H4 levels.
Coverage is limited by the existing native history, detector retention and level
limits. This is not an order-book map and does not prove the next destination.

## Stage
Show the actual engine gate: waiting for location/capture, M15 authorization,
causal POI/M5 refinement, prospective M5 retest, or observed retest. Historical
retested and already invalidated setups cannot appear as pending entries. Ludo
M15's last observed break is shown separately and explicitly does not authorize
an entry. This feature does not replace the current Lux major/internal gate.

## Delivery and isolation
`KAIROS_DAILY_BRIEF_ENABLED=true` opts in. Default is false. Daily context,
locations, liquidity and stage share the same paginated report. The source map
updates once per closed M5. New notifications occur on stage, capture/reaction,
daily review or Ludo M15 observation transitions, not on price changes alone.
Persistent SQLite at `/data/kairos_daily_brief.db` tracks transition occurrences
and acknowledges pages only after the existing Telegram sender returns True.
A→B→A is a new occurrence; retries/restarts of the same occurrence resume frozen
pages without resending acknowledged pages. Stale queued snapshots are skipped.
A crash between remote acceptance and local receipt commit can duplicate a page;
Telegram does not provide an idempotency key. Partial older context is superseded
when a newer state is queued; obsolete pages are not completed as current advice.
The queue is bounded to four pair slots. Scanner cache and delivery locks are
separate, so slow Telegram cannot block scanning through the presentation lock.
Legacy capture/setup/entry delivery and watermarks remain independent.

Native SOLUSDT smoke at export cutoff 22 Oct 2026 19:20 UTC: daily reading bearish
(high capture and inside close on 21 Oct), 57 supplied liquidity levels and 41 valid
H4/H1 zones. Existing decision and input candles remained exactly equal with
presentation off/on after removing the additive source field. Historical invalidated
LONG was correctly labeled invalidated rather than awaiting retest.
