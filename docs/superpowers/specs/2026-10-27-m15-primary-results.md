# M15 primary release verification

Feature: KAIROS_M15_PRIMARY_FVG=true. Monthly/weekly/daily map and daily bias unchanged. Relevant prior-period/H4/H1 liquidity + rejection/reclaim mandatory. Native Ludo math unchanged; closed break must pass existing experimental flexible directional2of3 body/tail/median test. ATR14 remains measurement only, not a gate. Native direct A/B/C M15 FVG required, C closed. Primary limit plan is available without M5 refinement or another break. M5 remains an early structure-only notice and optional refinement. No M1, order placement, stop-margin automation or performance guarantee.

## Tests and review

Built full suite with live flag set:407 passed +53 subtests;12 new primary tests and15 existing shadow tests. Offline reference tests explicitly isolate live flags; mode-specific tests opt in. Build patches passed; generated app.py/scalp_engine.py restored before commit.

Independent review found pre-breached protection on outside-bar breaks. Reproduced failing test, fixed both directions by rejecting ambiguous break and formation sources. Added migration/rollback retirement, optional-child independence, opposite M15 invalidation, reaction-change simultaneous touch and isolated optional-refinement errors. Optional error regression failed first, then fixed. Updated capture/daily text to make M5 optional. No deferred review findings.

## Actual archived candle replays

Fresh activation, no inherited DB, no exchange/Telegram calls, no fill assumptions. Same closed-prefix rolling-history causal replay as previous comparisons; primary flag enabled. Canonical source archives hash-verified by harness. Capture admission, reactions, native Ludo, native FVG/lifecycle and future retest active.

SOL26Oct00:00–27Oct00:00 Portugal:290 cycles.3 captures,4 early M5 events,3 strong M15 break records,1 M15 armed plan,1 observed retest,3 plan invalidations.06:00 weak break is excluded. A different LONG break at15:00 arms native M15 FVG101.71–102.00 at15:15; retest observed15:55; primary POI invalidated16:15. No fill/PnL claim. This example proves strength criteria do not eliminate losing/late setups or establish improved profitability.19:00 SHORT break confluent across2origins; no direct native M15 FVG at19:15, so no short plan.

BNB24Oct00:00–25Oct01:00 Portugal:302 cycles (25h archive window).5 captures,4 early M5 notices,4 strong M15 structural records,0 armed M15 plans,4 no-FVG plan invalidations. A strong candle without eligible causal chain/native direct FVG does not produce an entry plan. No claim that earlier demonstration M15/H1/H4 examples are new signals.

## Rollout and rollback

Production code rollback target:e9de3df3723b101c2509e917e4fc0833a6cd9e16 / Railway7d8e2781-97f0-4c8a-80ee-edcc8d647a37. Set KAIROS_M15_PRIMARY_FVG=false for reversible legacy behavior; incompatible tracked primary plans are retired once. Set true activates a prospective baseline, retires old legacy plans with manual cancellation notice, suppresses queued old actionable notices and leaves audit/acknowledgements intact. No exchange order is cancelled automatically.

Rollout verification: initial deployment312ae96d-7182-45e2-ad25-718eff5a1c77 SUCCESS, healthcheck passed, all4scanner pairs active. Found and fixed stale same-cutoff legacy summary before its next state transition: view now suppresses policy-mismatched plans immediately; existing delivery suppression already prevented stale actionable messages. Added runtime m15_primary telemetry. Regression failed first; final built suite408 +53,13 primary tests.
