# Daily bias and liquidity briefing — inactive candidate

This proposal is implemented in `kairos_daily_report.py` and
`kairos_daily_delivery.py`, with two guarded hooks in the existing scanner and
app delivery loop. It uses the SAME `send_telegram` callback, token and chat for
BTCUSD, ETHUSD, SOLUSD and BNBUSD. The hooks are inactive by default:
`KAIROS_DAILY_BRIEF_ENABLED` must explicitly be `true`. No live environment
variables have been changed. FVG/OB detectors, period levels, entry decisions,
existing alert messages and existing signal delivery watermarks are unchanged.

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

## Delivery through the existing bot

The pure formatter returns plain-text pages no longer than 3500 characters and
does not silently truncate levels. `prepare_notification` remains an isolated
preparation helper; it does not send. The integration's `scan_report` reads the
existing native candles, computes the existing registry once per successful
pair/day snapshot and keeps the structure context separate from daily bias.
It performs no additional market API calls. A mid-day startup reports the
current native day only, showing the actual map-observation timestamp; it does
not pretend that intraday captures were known at midnight.

`submit` uses a bounded background queue so daily briefing sends do not block
legacy signal delivery. `deliver` uses the EXISTING bot callback and records
each positively acknowledged page in `/data/kairos_daily_brief.db`, separate
from `alerts.db`. The same pair/day/rule payload is frozen durably for retries.
Failed pages retry on a later successful scan; acknowledged pages survive
restarts. As with the Telegram API generally, a crash after remote acceptance
but before local commit can duplicate one page (no remote idempotency key).
Storage failure prevents sending; report/send failures do not alter legacy
signal watermarks. No external message was sent during validation.

Before activation: validate the heuristic in replay and forward DEMO; finalize
price invalidation; check all four pairs with native data; rotate the Telegram
token following the Railway notice; deploy and explicitly enable only the
approved informational integration. No forecast-performance claim follows
from unit tests passing. Current live configuration remains untouched.
