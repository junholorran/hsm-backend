# Relevant HTF sweep and FVG entry correction

User clarified that frequency is not controlled by a timer or quota: entries must start from relevant prior day/week/month highs/lows or H4/H1 swing/EQ liquidity, followed by a sweep/reclaim, structural intent and causal FVG. M15/M5 are confirmation/refinement, not liquidity origins. Standalone HTF-FVG touches must not authorize plans.

- Add one shared whitelist: D1 PDH/PDL; W1 PWH/PWL; MN PMH/PML; H4/H1 SWING_HIGH/LOW, EQH/EQL, confirmed Ludo extremes. Exclude M15/M5 and standalone FVG touches from entry origin, preserving detectors and broad map as context.
- Add prior-period origin adapter using existing calendar levels and M15-confirmed first crossing only. Never reinterpret old D1 consumption as an intraday sweep.
- Keep rejection/reclaim and existing M15 structural confirmation; early M5 remains a structural heads-up under the same relevant origin, not an independent trade.
- Complete entry plan requires native causal Lux FVG M5 from the confirmed leg; no OB/IFVG fallback. Preserve native geometry/lifecycle and prospective retest timing. Filter FVG candidates before choosing latest so an IFVG cannot hide a valid FVG.
- Retire incompatible persisted plans/queued notices once, preserve audit, reject them in state view, retain valid H4/H1 rejection/FVG flows.
- Red/green regressions, full/build tests, independent review and verified production deploy. No cooldown, score, daily cap, RR/SL/TP or automatic execution.

## Verification

- Red regressions proved unwanted standalone-POI/local/macro origins, OB/IFVG fallback, old-view authority, truncated period history and invalid-latest-FVG selection before correction.
- Full suite after production build patches: 379 passed, 53 subtests passed. Native detector/geometry tests retained; legacy tests that authorized now-forbidden origins now explicitly assert exclusion.
- Native monthly calendar adapter tested with exact M15 raid clocks, no future confirmations, missing coverage rejection and older D1 no-cross proof. Daily-only consumption never becomes an intraday sweep.
- New-policy SOL26October replay: 290 closed-prefix evaluations; 3 relevant origin records (D1 PDL100.30, D1 PDH102.72, H1 Ludo high102.72); only1 FVG entry plan and1 observed retest. Local M15-only plans excluded; native early M5 notices remain non-entry observations.
- Independent review identified period-coverage and FVG-selection edges; both reproduced with failing tests and fixed in initial and later refinement paths. Final reviewer: no remaining blockers;32 policy tests independently passed.
- No automatic exchange orders or fabricated profitability. Publication and production verification follow this reviewed tree.
