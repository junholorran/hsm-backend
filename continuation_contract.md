# M5 continuation under a confirmed M15 thesis

Entry-only DEMO forward/replay follows new Lux internal-5 M5 BOS order blocks
after the complete M15/capture authorization is available. Existing initial
M5 refinement and the legacy SL/BE/TP engine are preserved.

Both closed M15 swing-50 and internal-5 states must match the direction at
authorization. A later contrary break in either stream ends eligibility at
its close. Missing M15/M5 bars reject continuation rather than infer history.

The new M5 BOS must be in the authorized direction. The broken pivot and OB
origin must be after authorization. Lux parsed-extreme OB formation is
computed using the prefix ending at the break. The OB must stay on the
authorized side of the protected M15 internal pivot. Its first eligible
retest must be after the BOS close. A close invalidating the OB is rejected.

Multiple continuation setups are intentional. The most recently confirmed
eligible continuation OB is the current candidate; a newer pending candidate
can supersede an older pending candidate. Every BOS confirmation has a
distinct setup key, even when price bounds match. Existing replay/forward
event storage retains prior armed and observed-retest records. A later
original-zone touch must not replace an earlier continuation retest.

FVG/IFVG original-leg refinement is unchanged; this extension specifically
covers newly formed BOS order blocks. It does not fabricate a new HTF capture,
M15 confirmation, SL, target, execution fill or P&L.

BTC historical regression, 7 October 2026 (Europe/Lisbon):

- M15 authorization: 09:45.
- OB 83,684.9–83,808.2: ready 12:40, first retest open 12:55,
  closed observation 13:00.
- OB 83,597.0–83,714.0: ready 13:25, first retest open 13:35,
  closed observation 13:40.

This is a historical causal-selection audit, not a claim of profitability or
universal indicator parity.
