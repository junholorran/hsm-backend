"""M15 primary manual plan helpers; native liquidity/structure remain unchanged."""
import os

LEGACY_STRONG_POLICY='M15_PRIMARY_FORCE_FVG_V1'
POLICY='M15_PRIMARY_LUDO_FVG_V2'


def enabled():
    return os.environ.get('KAIROS_M15_PRIMARY_FVG','').strip().lower()=='true'


def valid_break(candles,event):
    """Closed Ludo break, preserving protected-extreme safety without force gate."""
    if event.get('side') not in ('UP','DOWN'):
        return False
    if event.get('close_ts',0)-event.get('open_ts',0)!=900000:
        return False
    if event.get('level_known_ts',float('inf'))>event['open_ts']:
        return False
    candle=next((c for c in candles if c['t']==event['open_ts']),None)
    if candle is None:
        return False
    up=event['side']=='UP'
    if not (candle['c']>event['level'] if up else candle['c']<event['level']):
        return False
    return not (candle['l']<event['opposite_level'] if up else candle['h']>event['opposite_level'])


def strong_break(candles,event):
    """Classification only: the original balanced force metric controls the badge."""
    from kairos_fvg_force_shadow import _metrics
    if not valid_break(candles,event):
        return False
    rows=[c for c in candles if c['t']+900000<=event['close_ts']]
    index=next(i for i,c in enumerate(rows) if c['t']==event['open_ts'])
    return _metrics(rows,index,event['side'],900000)['experimental_balanced'] is True


def notice_is_strong(event):
    value=event.get('break_strong')
    if isinstance(value,bool):
        return value
    # Historical strict-policy events had force as an authorization gate.
    return event.get('primary_policy')==LEGACY_STRONG_POLICY


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
            and result.get('close_beyond_level',0)>0
            and z.get('invalidated_ts') is None):return z
    return None
