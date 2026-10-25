"""Recent intact Ludo extremes; separate from Lux50 and HTF FVG origins.

Only levels actually published by the existing closed-native Ludo structure
are considered. A completed right-hand native candle must confirm the local
turn before the first strict M15 crossing. No old level gets a new capture.
"""
import math

from kairos_ludo_structure import structure_snapshot

DURATIONS={'H4':14400000,'H1':3600000,'M15':900000}
M15=900000


def _closed(rows, duration, cutoff):
    cs=[c for c in rows if isinstance(c,dict) and isinstance(c.get('t'),(int,float))
        and math.isfinite(c['t']) and c['t']+duration<=cutoff]
    if (not cs or cs[-1]['t']+duration!=cutoff//duration*duration
            or any(b['t']-a['t']!=duration for a,b in zip(cs,cs[1:]))):
        return []
    if any(not all(isinstance(c.get(k),(int,float)) and math.isfinite(c[k]) for k in ('o','h','l','c'))
           or not 0<c['l']<=min(c['o'],c['c'])<=max(c['o'],c['c'])<=c['h'] for c in cs):
        return []
    return cs


def captures(tf_map, cutoff):
    """Return immutable first captures with availability preceding the raid.

    Formation, intactness and crossing are verified on contiguous closed data.
    Full M15 coverage starts at the native origin; otherwise fail closed. The
    first crossing is frozen even when a later excursion is more attractive.
    """
    m15=_closed(tf_map.get('M15') or [],M15,cutoff)
    if not m15:return []
    result=[];seen=set()
    for tf,duration in DURATIONS.items():
        native=_closed(tf_map.get(tf) or [],duration,cutoff)
        if not native:continue
        snapshot=structure_snapshot(native,duration,cutoff,include_levels=True)
        if snapshot['status']!='OK':continue
        positions={c['t']:i for i,c in enumerate(native)}
        for level in snapshot['levels']:
            origin=level['origin_ts'];idx=positions[origin];side=level['side'];price=level['level']
            identity=(tf,side,origin,price)
            if identity in seen:continue
            seen.add(identity)
            if idx==0 or idx+1>=len(native) or m15[0]['t']>origin:continue
            left,bar,right=native[idx-1:idx+2]
            pivot=(bar['h']>left['h'] and right['h']<=bar['h'] if side=='HIGH'
                   else bar['l']<left['l'] and right['l']>=bar['l'])
            if not pivot:continue
            known=max(level['known_ts'],right['t']+duration)
            # Search from the origin, not from confirmation: a level raided
            # before it was available must never be resurrected as intact.
            cross=next((c for c in m15 if c['t']>=origin+duration and
                        (c['h']>price if side=='HIGH' else c['l']<price)),None)
            if cross is None or cross['t']<known:continue
            closed=cross['t']+M15
            if level['active_until_ts'] is not None and closed>level['active_until_ts']:continue
            result.append(dict(origin_kind='LUDO_SWING_CAPTURE',structural_source='LUDO',
                liquidity_tf=tf,liquidity_type='LUDO_SWING_HIGH' if side=='HIGH' else 'LUDO_SWING_LOW',
                nivel=price,liquidity_side=side,liquidity_origin_ts=origin,
                liquidity_confirm_ts=known,native_capture_confirm_ts=closed,
                capture_confirm_ts=closed,capture_evidence_tf='M15',capture_tf='M15',
                first_capture_ts=cross['t'],sweep_ts=cross['t'],
                extremo=cross['h'] if side=='HIGH' else cross['l'],
                capture_confirmation_basis='CLOSED_M15_CROSS_OF_CONFIRMED_LUDO_EXTREME'))
    return sorted(result,key=lambda c:(c['native_capture_confirm_ts'],c['liquidity_tf'],c['nivel']))
