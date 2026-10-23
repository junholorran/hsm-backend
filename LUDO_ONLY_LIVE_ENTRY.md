# Ludo as the sole live structural entry provider

`KAIROS_ENTRY_STRUCTURE=LUDO` routes the validated latest-closed scanner directly
to `kairos_ludo_entry.snapshot`, before evaluating the legacy decision layer or
observing any of its cached phase records. Unsupported nonempty policy values
fail closed. Provider/storage/data failures do not fall back to Lux authorization.

The active manual sequence is: known HTF liquidity capture OR a distinct HTF FVG
interaction → closed M15 Ludo BOS/CHoCH → causal M15 POI → M5 refinement →
prospective retest. The supplied lookback-10 Ludo math is unchanged. Its native
M15 body-close break fixes the directional leg; there is no Major50/Internal5
alignment gate for live entry. M5 refines the authorized M15 leg.

Lux map, liquidity pool, FVG/IFVG and parsed-OB calculations remain unchanged.
They supply context and zone geometry/lifecycle, not Major/Internal directional
entry permission. Touch discovery was extracted verbatim from the existing HTF
FVG path and is shared by offline references and the active provider. A touch is
never labelled as a liquidity capture. HTF origin invalidation retires its chain,
including an origin still awaiting its first M15 break.

The live provider's legacy signal and phase collections are empty. Setup and
retest messages come only from the durable Ludo chain outbox. Capture messages
and daily stages explicitly use the Ludo model; cached legacy setups cannot set
the displayed entry stage. Retests remain observations, with no assumed fill,
automatic order, stop/TP or real-money execution. Historical retests are labelled
historical. Stored manual stops are not observable from exchange data.

The old Lux evaluator/replay remains available as an offline reference, not the
active live scanner. This change does not rewrite historical reference results
or claim Ludo/TradingView parity with unmatched candle histories. Disable the
policy only for an intentional rollback; there is no automatic fallback.

Persistence reuses `/data/kairos_capture_chains.db`. Existing Ludo capture chains
continue. A separate activation baseline admits only new HTF FVG interactions,
so enabling this origin does not replay an old touch as a fresh setup. State and
outbox commit together; failed Telegram sends retry durably. Daily/radar
presentation failures cannot suppress durable entry-chain delivery.

Validation: 266 tests and 53 subtests pass, including legacy-reference tests,
independent chains, native SOL replay, provider/old-state isolation, HTF origin
identity/lifecycle, restart baseline, historical stages and optional-export
failure isolation. A native SOL provider smoke at 21 October 19:15 UTC reaches
SHORT/awaiting M5 retest with legacy entry authorization functions set to fail if
called. Offline legacy-reference tests remain unchanged and green.

Production-environment check: the offline-reference suite is isolated from the inherited live entry policy by a pytest-only fixture. Individual Ludo tests explicitly opt in. The fixture never runs in the application process. The full suite also passes with all three production feature flags enabled (266 tests, 53 subtests).
