"""M15 primary manual plan helpers; native liquidity/structure remain unchanged."""
import os

POLICY='M15_PRIMARY_FORCE_FVG_V1'


def enabled():
    return os.environ.get('KAIROS_M15_PRIMARY_FVG','').strip().lower()=='true'


def strong_break(candles,event):
    from kairos_fvg_force_shadow import _metrics
    rows=[c for c in candles if c['t']+900000<=event['close_ts']]
    index=next((i for i,c in enumerate(rows) if c['t']==event['open_ts']),None)
    if index is None:return False
    candle=rows[index]
    if (candle['l']<event['opposite_level'] if event['side']=='UP' else candle['h']>event['opposite_level']):
        return False
    return _metrics(rows,index,event['side'],900000)['experimental_balanced'] is True


def direct_zone(candles,event,cutoff,engine):
    from kairos_fvg_force_shadow import assess_fvg
    rows=[c for c in candles if c['t']+900000<=cutoff]
    for z in engine._kairos_fvg_states(rows,lookback=len(rows)):
        if (not (z.get('ludo_fvg') is True or z.get('lux_fvg') is True) or z.get('tipo') not in ('FVG_bullish','FVG_bearish')
            or (z.get('source_mid') or {}).get('t')!=event['open_ts']):continue
        sources=[z.get(k) or {} for k in ('source_a','source_mid','source_c')]
        if any((c.get('l',float('-inf'))<event['opposite_level'] if event['side']=='UP'
                else c.get('h',float('inf'))>event['opposite_level']) for c in sources):continue
        result=assess_fvg(rows,z,event,900000,cutoff)
        if (result.get('geometry_valid') and result.get('direct_break')
            and result['middle']['experimental_balanced'] is True
            and z.get('invalidated_ts') is None):return z
    return None
