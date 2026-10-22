# Daily bias and liquidity briefing — inactive candidate

This proposal is isolated in `kairos_daily_report.py`. No existing application,
scanner, Telegram sender, FVG/OB detector, period levels, entry authorization or
production database is changed. There is no scheduled callback and nothing is
sent. Native candles and registry snapshots must be supplied by a future caller.

## Explicit daily rule

Compare the latest complete UTC D1 with the immediately preceding D1. Strict
high/low exceedances are captures. Double capture has first priority: NEUTRO,
even if the close ends outside. Otherwise a close above the previous high is
BULLISH; below the previous low is BEARISH; a high-only capture with a close
strictly inside is BEARISH; low-only capture with an inside close is BULLISH.
Other cases, including exact boundary closes, are NEUTRO. Missing, stale or
malformed days cannot produce a notification. The hypothesis applies only to
the next native UTC day and is available from its opening timestamp.

This rule is a proposed heuristic, not a complete ICT methodology or a validated
forecast. The earlier BNB test of Lux50 structural bias does not validate this
different rule. No profitability or directional accuracy is claimed here.
There is no validated intraday price-invalidation rule yet; the payload says so
rather than manufacturing a price. Expiry at the following UTC day is temporal
validity, not structural invalidation. Price-action confirmation remains M15,
then M5 refinement; this report never authorizes entry.

## Existing map, unchanged

`build_report` consumes a precomputed `_kairos_structural_registry` snapshot with
the same cutoff. It copies every supplied H4/H1 swing or EQH/EQL and the six
PDH/PDL/PWH/PWL/PMH/PML references, preserving origins, confirmation and capture
metadata. It does not rerun or change detectors, fabricate missing levels or
turn FVG/OB into liquidity. Future evidence/time mismatches fail closed. Active
and captured states are inherited from the registry, not independently verified.

Current upstream coverage limits remain: up to 40 structural swings per TF,
up to 30 equal levels per TF, and the available native history. Therefore
"all" means all selected levels supplied by that bounded registry, not all
historical liquidity or exchange orders. Counts and missing period types are
shown. Nearest available levels above/below the last D1 close are descriptive;
they are not live-price references, targets or a predicted destination.

## Delivery preparation

The pure formatter returns plain-text pages no longer than 3500 characters and
does not silently truncate levels. `prepare_notification` only stores candidate
payloads once per pair/UTC-day/rule-version in an explicitly supplied SQLite
connection. It never reads environment secrets, opens the production database,
contacts Telegram or marks delivery successful. A preparation record is not a
delivery receipt. The caller owns transactions.

Before activation: validate the heuristic in replay and forward DEMO; finalize
price invalidation; review scheduling and gap/catch-up policy; add reliable
multipart Telegram delivery/retry receipts; use a separate database namespace;
check all four pairs; rotate the Telegram token following the Railway notice;
deploy only the approved integration. No extra market API traffic is required
by this candidate. Current live configuration remains untouched.
