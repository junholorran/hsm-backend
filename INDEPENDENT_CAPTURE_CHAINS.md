# Independent capture observation

`KAIROS_CAPTURE_CHAINS_ENABLED=true` enables an additive observer for BTCUSD,
ETHUSD, SOLUSD and BNBUSD. Default is false. It consumes the existing validated,
closed M15/M5 candles and structural capture audit, with no additional fetches.
Existing decision results, daily map, FVGs, period levels and entry phases are unchanged.

Each H1/H4/D1/W1 capture has its own identity (pair, timeframe, pool type,
origin, price, first capture). Later captures never inherit an earlier break.
Monthly references remain context in the existing map.

Sequence:

1. Capture known from closed M15 evidence: observe, without presuming reversal.
2. Ludo M15 body-close CHoCH/BOS after capture confirmation, in either direction.
3. Existing causal M15 FVG/IFVG/OB selector; no zone means no armed plan.
4. Existing causal M5 refinement, or a later Ludo M5 break producing an
   overlapping causal zone. FVG source candles must belong to this capture/leg.
5. Freeze prospective M5 zone at actual observation time, not the candle cutoff.
6. Notify a subsequent closed-bar contact, invalidation, or ambiguous
   touch+invalidation. No order, fill, guaranteed opportunity, or profitability is inferred.

Native FVG/IFVG lifecycle is read by zone id, preserving wick/body deletion and
buffer eviction. OB uses existing parsed-extreme formation and High/Low deletion,
plus supplied removal metadata. These observation-specific Ludo breaks are not a
claim of equivalence to Lux's independently generated structure/OB queues.
Opposite M15 breaks and parent POI deletion invalidate the plan. A continuation
break is notified while an existing valid M5 zone is preserved. A later break
can form a new plan within the same capture; another capture is always independent.
Protected M15 extrema are invalidation references, not automatically placed stops.
Manual exchange stop-outs are not visible to this observer.

Separate `/data/kairos_capture_chains.db` state and outbox are transactionally
committed together. Delivery uses the existing Telegram sender in a background
worker; failed sends retry without advancing the acknowledgement. A crash after
Telegram accepts but before SQLite acknowledges can duplicate a message.
Late deliveries are labelled historical. First activation establishes a baseline
and does not replay old captures. Restart resumes existing chains and pending
messages. Missing/gapped native history blocks observation; a capture no longer
covered by rolling history is closed with an explicit insufficient-history notice.

Validation includes independent-capture regressions, same-bar ambiguity,
opposite-break ordering, native FVG wick deletion, expired parent rejection,
causal source candles, actual arming time, restart/retry and unchanged legacy
results on observer failure. SOL native fixture on 21 October 2026 reproduces
M15 bearish CHoCH at 19:15 UTC / 20:15 Portugal across 107.87, then an observed
M5 zone/retest. This is causal replay evidence, not empirical trading edge.

Rollback: disable the flag and redeploy. Existing legacy state is untouched;
retain the separate database to resume or audit chains later.
