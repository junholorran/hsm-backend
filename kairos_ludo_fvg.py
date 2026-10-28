# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. https://mozilla.org/MPL/2.0/
# Derived from SMC Structures and FVG, © LudoGH68, supplied by the user.
"""Ludo FVG on closed native candles, with audit records for removed boxes.

Pine high[3]/low[1] is ABC on the preceding three bars: available when C
closes / the next bar opens. No future bar's OHLC is needed for formation.
Subsequent mitigation is observed on closed bars, not intrabar like Pine.
Defaults mirror the supplied script, including its literal history+1 cap.
IFVG is NOT part of Ludo; the existing Lux IFVG extension stays separate.
"""
from kairos_lux_fvg import _mother, inversion_fvg_states


def fvg_states(candles, lookback=250, history=5, reduce_mitigated=False):
    states=[]
    active=[]
    capacity=max(1,min(50,int(history)))+1
    for i,c in enumerate(candles):
        for z in list(active):
            bull=z['direcao']=='alta'
            full=c['l']<=z['bottom'] if bull else c['h']>=z['top']
            partial=c['l']<z['top'] if bull else c['h']>z['bottom']
            if full:
                z.update(state='INVALIDADA',invalidated_ts=c['t'],ludo_displayed=False,
                         mitigated_ts=c['t'],invalidation_basis='LUDO_FULL_MITIGATION')
                active.remove(z)
            elif partial:
                if z['first_touch_ts'] is None:z['first_touch_ts']=c['t']
                z['state']='TOCADA'
                if reduce_mitigated:z['top' if bull else 'bottom']=c['l' if bull else 'h']
        if i<2:continue
        a=candles[i-2]
        bull=a['h']<c['l']
        bear=a['l']>c['h']
        if not (bull or bear):continue
        z=_mother(candles,i,bull,'LUDO_FVG')
        z.update(ludo_fvg=True,lux_fvg=False,lux_ifvg=False,
                 fvg_reference='LUDO_GH68_USER_PINE_V5',ludo_displayed=True,
                 original_top=z['top'],original_bottom=z['bottom'],
                 reduce_mitigated=bool(reduce_mitigated))
        states.append(z);active.append(z)
        if len(active)>capacity:
            old=active.pop(0)
            old.update(state='INVALIDADA',invalidated_ts=c['t'],ludo_displayed=False,
                       invalidation_basis='LUDO_HISTORY_EVICTION')
    minimum=candles[max(0,len(candles)-max(1,int(lookback)))]['t'] if candles else 0
    return [z for z in states if z['state']!='INVALIDADA' or z['created_ts']>=minimum]


def combined_fvg_states(candles, lookback=250):
    minimum=candles[max(0,len(candles)-max(1,int(lookback)))]['t'] if candles else 0
    inv=[z for z in inversion_fvg_states(candles) if z['state']=='IFVG' or z['created_ts']>=minimum]
    return sorted(fvg_states(candles,lookback)+inv,key=lambda z:(z['created_ts'],z['id']))

print("[KAIROS_LUDO_FVG] reference=USER_PINE_V5 history=5 reduce=false full_mitigation=INCLUSIVE closed_native=true lux_period_levels=UNCHANGED",flush=True)
