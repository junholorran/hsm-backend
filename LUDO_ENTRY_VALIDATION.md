# Ludo structure entry validation — first integration stage

The supplied LudoGH68 `SMC Structures and FVG` structure block is reproduced in
`kairos_ludo_structure.py` under MPL-2.0. Lookback is 10; breaks use CLOSED candle
closes. The function-local persistent index, loop overwrite order, and three
previous-close conditions are retained. This is a structure port, not an ICT
certification or a universal chart parity claim. TradingView's forming candle
and different initial history can differ.

The scanner records one SHADOW_ONLY observation per pair/M5 cutoff from native
candles it already fetched. M5/M15/M30/H1/H4/D1/W1 each use their own candles.
The existing result is NOT replaced or modified. FVG/OB detectors, liquidity
capture/period map, signal authorizations, senders and setup tracking are intact.
GET `/experiment/ludo_structure_shadow` reads cached evidence only.

BNBUSDT Bybit native fixture, cutoff 2026-10-21 10:15 UTC:
- M5 low 744.91, origin 08:55 UTC, established by BOS closing 09:15 UTC.
- M15 low 742.84, origin 06:45 UTC.
- H1 low 739.40, origin 02:00 UTC, established by BOS closing 08:00 UTC.
These levels were compared with the user's chart. This does not validate every
historical level or future signal. Snapshot does not contain a closed loss of
744.91; a test appends explicitly synthetic bars to verify close/wick behavior.

The optional shadow reversal candidate identifies the first Ludo M5 CHoCH after
a confirmed HTF capture and prior M5 reclaim. It is explicitly
BREAK_OBSERVED_NOT_AN_ENTRY. Displacement, causal POI, prospective retest and
invalidation still need integration and independent replay validation before
this observer may authorize an entry. It does NOT replace continuation or
HTF-FVG-touch rules. No trade, fill, stop, target or performance is inferred.

Validation: pytest uses native regression levels, cutoff truncation, missing
bars, wick-vs-close, synthetic first loss, causal capture/reclaim, nonmutation,
and once-per-cutoff caching. The normal Railway suite must also pass.
