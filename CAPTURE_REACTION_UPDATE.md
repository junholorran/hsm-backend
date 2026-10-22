# Capture reaction update

Scope: primary W1/D1/H4/H1 and previous-period liquidity captures returned by
`_kairos_select_structural_first_capture_sweep`. Monthly context-only radar
classification remains unchanged. Existing FVG, OB, liquidity level and structure
detectors are unchanged.

After a proven first capture, inspect only closed, contiguous M15 candles.
Two consecutive closes strictly above a high confirm acceptance; two below
confirm rejection/reclaim. A low uses the symmetric rule. Mixed closes and exact
level closes do not establish a new state. Preserve the last confirmed state
until an opposite pair confirms; never move the original first-capture timestamp.
A gap cannot form a confirming pair. Later adjacent closed bars may establish
a new reaction without inferring any close inside the missing interval.

`confirm_ts` is the OPEN of the second confirming candle, as in the existing
interface. `reaction_confirm_ts` is its CLOSE and the actual availability time.
Only actual state changes move these timestamps. Recomputing a historical prefix
never sees future bars. Each transition has its own radar key; repeated snapshots
of the same transition retain that key and timestamp.

M15 internal confirmation cannot precede the new reaction confirmation. A break
on the second confirming candle is available at the same close and may qualify.
Lux major authorization remains required. This patch does not activate Ludo
entries or turn a capture/reaction into an automatic trade. Existing prospective
setup tracking is not redesigned by this patch.

Native SOLUSDT regression, 21 October 2026, Europe/Lisbon:
- first capture of PDH 108.22 at 16:00 (known at 16:15);
- 16:00 candle close 108.04, next close 108.35: unresolved at 16:30;
- next close above confirms acceptance at 16:45;
- 19:45 close 108.05 and 20:00 close 107.71 confirm rejection at 20:15;
- original capture stays 16:00 throughout.

The 20:15 break of 107.87 remains insufficient for a SHORT under the unchanged
Lux major rule; that major break at 104.92 occurred at 07:30 on 22 October.
The fixture proves reaction timing, not profitability or an executable fill.
