# FVG force observation — archived SOL / BNB

Research only. No production deployment, no new entry filter, no change to Ludo/liquidity/Telegram. Thresholds (60% body, tail <=25%, body >=1.5 prior 20-body median) are unvalidated Kairos hypotheses, not official ICT rules. Third-candle color is not a gate. Volume is not a gate. No claim of better performance or guaranteed institutional intent.

## SOL — 26 October 2026, Portugal (UTC)

Existing liquidity-qualified replay already identified prior D1 low 100.30, first capture confirmed 03:45, reclaim 04:30, CHoCH M15 confirmed 06:00 and selected M5 FVG 100.41–100.71. This new observer does not rerun liquidity qualification or reconstruct Telegram.

The actual M15 05:45–06:00 candle: O101.36 H101.50 L101.30 C101.42. Structure level 101.39; close beyond level 0.03. Body0.06 =30% of range; closing upper tail40%; body0.375 times prior median0.16. It fails the experimental strength hypothesis while remaining a valid Ludo close-break.

Parent M15 FVG 100.25–100.53 uses A03:45, B04:00, C04:15; confirmed04:30. Its B body56c =87.5% of range, 4x median. M5 FVG100.41–100.71 uses A04:05, B04:10, C04:15; confirmed04:20. Its B body0.35 =85.37% of range, 2.69x median. Both gaps are genuine native geometry earlier in the causal leg, not a gap with the06:00 M15 break candle as the middle. No direct native M5 break coincides with the selected M5 B candle. Strong earlier impulse is not proof of strong later M15 break.

## BNB — archived M30 example, 22 October 2026

Native Ludo CHoCH DOWN: breaking candle opened02:00UTC /03:00Portugal and closed02:30UTC /03:30Portugal, level743.34, close741.00. It passes the experimental body test: body2.80, body/range64.97%, closing lower tail16.01%, 3.46x prior-body median0.81.

Native M30 FVG742.39–743.80 centered on that break has A01:30UTC, B02:00UTC, C02:30UTC. Third closes03:00UTC /04:00Portugal. It is a direct-break FVG and passes the experimental hypothesis. This is a structural/FVG observation, not a verified liquidity-qualified SHORT or proof of TradingView parameter parity. The screenshot alone does not establish the exact captured HTF pool.

## Bounded comparison

SOL26Oct00:00–27Oct00:00UTC:28 Ludo events across M5/M15/M30,11 direct-break FVG associations,11 passing experimental candidates.
BNB21Oct23:00–25Oct21:30UTC (22Oct00:00 Portugal start):74 events across M5/M15/M30,27 direct associations,24 passing. These counts are NOT entry signals: liquidity qualification and lifecycle eligibility were not reapplied by this observer. Association counts can include the same gap under different structural events/timeframes.

Both archives have gap-free native M5/M15/M30 histories and canonical SHA256 verified before analysis. SOL SHA256:9a0de16443e5f529a916480229def77537f8dfcf22ccf5ebacc29ec32242e33c. BNB SHA256:2235c67d770d7f661ec0159a205b9d94616df4bc3fcb5abdd0b6a8da277245dc. No fresh fetch or exchange/Telegram/database calls.

This structural comparison uses the entire available archived prefix rather than the production rolling-page limits. It is not a substitute for the existing production-equivalent causal replay. Ludo initialization/history can change events. Observations are made on closed prefixes through the third candle, including the next candle when B is the break. This stricter single-B hypothesis does not encompass all possible multi-candle displacement interpretations.

## Activation

Optional provider diagnostic: KAIROS_FVG_FORCE_SHADOW=true. Default off. Adds only fvg_force_shadow to the returned report; no outbox, decision or saved-plan writes. Diagnostic exceptions are isolated as OBSERVER_ERROR. Not deployed/activated by this task. Replay CLI: tools/replay_fvg_force_shadow.py SOURCE OUTPUT --start EPOCH_MS --end EPOCH_MS.

## ATR V2 extension — 27 October 2026

Prior-only Wilder ATR14 now recorded, not used as a filter. SOL M15 breaking body0.06 / priorATR0.457181 =0.1312ATR. Selected earlier SOL M5 FVG B body0.35 / priorATR0.289897 =1.2073ATR; gap0.30 =1.0348ATR. BNB M30 breaking B body2.80 / priorATR2.166374 =1.2925ATR; gap1.41 =0.6509ATR. Compare within timeframe/context; an M30 vs M15 distinction is not proof of comparative trading edge.

For margin rather than an all-or-nothing body requirement, V2 reports both experimental strict (all3 existing body/tail/median checks) and balanced (at least2of3, plus directional body). Same direct-break FVG associations: SOL11/11 strict,11/11 balanced; BNB24/27 strict,27/27 balanced. These are not liquidity-qualified entry counts and do not establish that balanced is better. All native FVG candidates remain intact; no quality label enables or disables a signal.

Tests:15 targeted tests including prior-only ATR, scale invariance, missing history/gaps; full built suite394 passed +53 subtests. Production unchanged.

Independent ATR V2 review: no blockers; requested additional two-of-three/opposite-direction coverage added. Final source suite395 passed +53 subtests. Built suite394 +53 before that test-only addition.
