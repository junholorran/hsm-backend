"""Closed-native-bar ports of LuxAlgo SMC FVG and Inversion FVG.

References: official LuxAlgo Library source, Pine v6, retrieved 2026-10-14.
SMC: /library/indicator/smart-money-concepts-smc/
IFVG: /library/indicator/inversion-fair-value-gaps-ifvg/
These are distinct published indicators, with distinct mother filters.
Removed records are retained for evidence, never exposed as active POIs.
"""

def _mother(candles, i, bullish, prefix):
    a,b,c=candles[i-2:i+1]
    return {'id':f'{prefix}_{b["t"]}_{"B" if bullish else "S"}',
            'tipo':'FVG_bullish' if bullish else 'FVG_bearish',
            'direcao':'alta' if bullish else 'baixa',
            'top':c['l'] if bullish else a['l'], 'bottom':a['h'] if bullish else c['h'],
            'created_ts':c['t'],'origin_ts':b['t'],'state':'ATIVA',
            'source_a':dict(a),'source_mid':dict(b),'source_c':dict(c),
            'flip_ts':None,'flip_candle':None,'mother_fvg_id':None,
            'first_touch_ts':None,'mitigated_ts':None,'invalidated_ts':None}


def smc_fvg_states(candles, lookback=250):
    states=[];cumulative=0.;retain=max(2,len(candles)-max(1,int(lookback)))
    for i in range(1,len(candles)):
        b=candles[i-1]
        delta=(b['c']-b['o'])/(b['o']*100.) if b['o'] else 0.
        cumulative+=abs(delta)
        if i<2:continue
        a,c=candles[i-2],candles[i];threshold=cumulative/i*2.
        bullish=c['l']>a['h'] and b['c']>a['h'] and delta>threshold
        bearish=c['h']<a['l'] and b['c']<a['l'] and -delta>threshold
        # Pine deletes existing boxes before detecting this bar's new mother.
        for z in states:
            if z['state']=='INVALIDADA':continue
            # SMC stores bearish .top=currentHigh, NOT normalized upper bound.
            # Preserve that literal source field for deletion, and normalize
            # coordinates separately for zone/entry consumers.
            removed=c['l']<z['smc_raw_bottom'] if z['direcao']=='alta' else c['h']>z['smc_raw_top']
            if removed:
                z.update(state='INVALIDADA',invalidated_ts=c['t'],lux_displayed=False,
                         invalidation_basis='LUX_SMC_WICK_DELETE')
                continue
            contact=c['l']<=z['top'] if z['direcao']=='alta' else c['h']>=z['bottom']
            if contact:
                z['first_touch_ts']=z['first_touch_ts'] or c['t']
                z['state']='TOCADA'
        if bullish or bearish:
            z=_mother(candles,i,bullish,'FVG')
            z.update(lux_fvg=True,lux_ifvg=False,lux_reference='LUX_SMC_V6',
                     smc_raw_top=c['l'] if bullish else c['h'],
                     smc_raw_bottom=a['h'] if bullish else a['l'],lux_displayed=True,
                     lux_bar_delta_percent=delta,lux_threshold=threshold,
                     lux_middle_close_condition=True,lux_geometry_condition=True)
            states.append(z)
    return [z for z in states if z['state']!='INVALIDADA' or z['created_ts']>=candles[retain]['t']] if len(candles)>retain else states


def inversion_fvg_states(candles, atr_mult=0.25, show_last=5):
    # Lux defaults: 100-element buffer for each mother/inversion side,
    # trimmed at >=100 before processing; Show Last affects drawings only.
    mothers={1:[],-1:[]};inversions={1:[],-1:[]};archive=[]
    ranges_sum=0.;atr=None;true_ranges=[]
    for i,c in enumerate(candles):
        tr=c['h']-c['l'] if i==0 else max(c['h']-c['l'],abs(c['h']-candles[i-1]['c']),abs(c['l']-candles[i-1]['c']))
        true_ranges.append(tr);ranges_sum+=c['h']-c['l']
        if i==199:atr=sum(true_ranges)/200.
        elif i>=200:atr=(atr*199.+tr)/200.
        # nz(ATR200*mult, cum(high-low)/(bar_index+1)); multiplier
        # is intentionally absent from the fallback in the official source.
        width_threshold=atr*atr_mult if atr is not None else ranges_sum/(i+1)
        if i>=2:
            a,b=candles[i-2:i]
            bull=c['l']>a['h'] and b['c']>a['h']
            bear=c['h']<a['l'] and b['c']<a['l']
            if bull or bear:
                width=c['l']-a['h'] if bull else a['l']-c['h']
                if width>width_threshold:
                    z=_mother(candles,i,bull,'IFVG_MOTHER')
                    z.update(lux_fvg=False,lux_ifvg=True,lux_reference='LUX_IFVG_V6',
                             lux_ifvg_width_threshold=width_threshold,lux_ifvg_atr_mult=atr_mult,
                             lux_middle_close_condition=True,lux_geometry_condition=True,
                             lux_retest_events=[],lux_displayed=False,_dir=1 if bull else -1,_state=0)
                    mothers[1 if bull else -1].append(z)
        body_top=max(c['o'],c['c']);body_bottom=min(c['o'],c['c'])
        for side in (1,-1):
            queue=mothers[side]
            if len(queue)>=100:queue.pop(0)
            for k in range(len(queue)-1,-1,-1):
                z=queue[k]
                breach=body_bottom<z['bottom'] if side==1 else body_top>z['top']
                if breach:
                    queue.pop(k)
                    z.update(mother_fvg_id=z['id'],flip_ts=c['t'],flip_candle=dict(c),state='IFVG')
                    inversions[side].append(z)
        for side in (1,-1):
            queue=inversions[side]
            if len(queue)>=100:
                z=queue.pop(0);z.update(state='INVALIDADA',invalidated_ts=c['t'],invalidation_basis='LUX_IFVG_BUFFER_EVICTION');archive.append(z)
            for k in range(len(queue)-1,-1,-1):
                z=queue[k];direction=z['_dir'];stage=z['_state']
                if stage==0:
                    z['_state']=1;z['_dir']=-direction
                    z['tipo']='IFVG_bullish' if -direction==1 else 'IFVG_bearish'
                    z['direcao']='alta' if -direction==1 else 'baixa'
                if stage>=1:
                    prev_close=candles[i-1]['c'] if i else None
                    retest=(direction==-1 and c['c']<z['bottom'] and prev_close>=z['bottom'] and prev_close<z['top']) or (direction==1 and c['c']>z['top'] and prev_close<=z['top'] and prev_close>z['bottom'])
                    if retest:z['lux_retest_events'].append(c['t'])
                    breach=body_top>z['top'] if direction==-1 else body_bottom<z['bottom']
                    if breach:
                        queue.pop(k);z.update(state='INVALIDADA',invalidated_ts=c['t'],invalidation_basis='LUX_IFVG_BODY_DELETE');archive.append(z)
                    elif (c['h']>=z['bottom'] if direction==-1 else c['l']<=z['top']):
                        z['first_touch_ts']=z['first_touch_ts'] or c['t']
    for queue in inversions.values():
        for i,z in enumerate(queue):z['lux_displayed']=i>len(queue)-1-show_last
    out=archive+inversions[1]+inversions[-1]
    for z in out:z.pop('_dir',None);z.pop('_state',None)
    return out


def combined_fvg_states(candles, lookback=250):
    minimum=candles[max(0,len(candles)-max(1,int(lookback)))]['t'] if candles else 0
    smc=smc_fvg_states(candles,lookback)
    inv=[z for z in inversion_fvg_states(candles) if z['state']=='IFVG' or z['created_ts']>=minimum]
    return sorted(smc+inv,key=lambda z:(z['created_ts'],z['id']))

print('[KAIROS_LUX_FVG] installed=true smc_delete=WICK ifvg_flip=BODY '
      'ifvg_atr_multiplier=0.25 closed_native_inputs=true lifecycle_corrected=true')
