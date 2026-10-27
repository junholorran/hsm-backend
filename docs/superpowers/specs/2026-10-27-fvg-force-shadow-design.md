# FVG force observation

User authorized implementing the discussed observation-first comparison. Preserve liquidity eligibility, Ludo, native FVG geometry/lifecycle, causal plans, outbox and Telegram. No production deployment or signal filtering in this task.

Use a pure observer and an optional snapshot diagnostic (KAIROS_FVG_FORCE_SHADOW=true, default off). Never mutate inputs. Catch observer failures outside the trading path. Record A/B/C, middle-body metrics, break-candle metrics, same-timeframe direct-break association versus earlier gap inside causal leg. Do not confuse a M5 FVG with a M15 break candle. Third candle color/strength is not required. Metrics use only fully closed bars and 20 bars before the measured candle; unknown history is not failure. Experimental thresholds: body/range >=0.60, directional closing tail/range <=0.25, body/prior median >=1.50; these are not official ICT rules. Flat baseline is unknown.

Chosen approach: observation, not immediate filtering (unvalidated) or changing Ludo (outside scope). Compare real archived SOL and BNB candles with explicit source hashes, coverage and timestamps. No fill/profit inference. Report existing SOL plan separately from hypothetical stronger criteria. Never infer liquidity from structural events alone.

## V2 research extension requested by user

Add Wilder ATR14 measured strictly before the evaluated candle; first TR high-low, seed average14 TR, then RMA14 over contiguous prior segment. A recent data gap or insufficient/zero ATR is unknown. Report body/ATR, range/ATR and FVG width/ATR (using B's prior ATR). ATR is a measurement, not a new threshold/gate. Preserve strict all3 hypothesis and expose a separate experimental balanced2of3 hypothesis with directional body required. These are research comparisons, not validated FVG quality grades or changed entries. The combined liquidity/structure/lifecycle gates remain authoritative and unchanged. No production activation. Multi-candle aggregate displacement is not implemented by this extension.
