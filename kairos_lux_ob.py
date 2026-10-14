"""LuxAlgo SMC v6 default High/Low order-block lifecycle.

Reference: https://www.luxalgo.com/library/indicator/smart-money-concepts-smc/
Source snapshot SHA256: a8046ad353c1b495e3c31e79365d7658a295be743ba56293e7d4c861813af48b
Formation is supplied by the existing parsed-extreme detector. Closed native
bars only; removed records are retained as inactive audit evidence.
"""

def ob_invalidated(zone, candle, mitigation='High/Low'):
    if mitigation not in ('High/Low', 'Close'):
        raise ValueError('Unknown LuxAlgo OB mitigation')
    bull=zone.get('direcao')=='alta' or zone.get('tipo')=='OB_bullish'
    bear=zone.get('direcao')=='baixa' or zone.get('tipo')=='OB_bearish'
    if not (bull or bear):
        raise ValueError('OB direction missing')
    if mitigation=='Close':
        return candle['c'] < zone['bottom'] if bull else candle['c'] > zone['top']
    return candle['l'] < zone['bottom'] if bull else candle['h'] > zone['top']

def order_block_states(candles, events, formation, lookback=300):
    """storeOrdeBlock then deleteOrderBlocks; one queue capped at 100."""
    if not candles:
        return []
    by_index={}
    for event in events:
        by_index.setdefault(event['index'],[]).append(event)
    queue=[];archive=[]
    for i,candle in enumerate(candles):
        for event in by_index.get(i,[]):
            ob=formation(candles,i,event['direcao'],structure=event)
            if not ob or ob['top'] < ob['bottom']:
                continue
            if len(queue)>=100:
                old=queue.pop();old.update(state='REMOVIDA_CAPACIDADE',removed_ts=candle['t'])
                archive.append(old)
            queue.insert(0,{**ob,'break_ts':event['t'],'break_tipo':event['tipo'],
                'state':'ATIVA','invalidated_ts':None,'first_touch_ts':None,
                'mitigation':'High/Low','lux_reference':'LUX_SMC_V6'})
        survivors=[]
        for ob in queue:
            if ob_invalidated(ob,candle):
                ob.update(state='INVALIDADA',invalidated_ts=candle['t'])
                archive.append(ob)
            else:
                if candle['t']>ob['break_ts'] and candle['h']>=ob['bottom'] and candle['l']<=ob['top'] and ob['first_touch_ts'] is None:
                    ob['first_touch_ts']=candle['t']
                survivors.append(ob)
        queue=survivors
    lower=candles[max(0,len(candles)-max(1,int(lookback)))]['t']
    return sorted(queue+[z for z in archive if (z.get('removed_ts') or z.get('invalidated_ts') or 0)>=lower],
                  key=lambda z:(z['break_ts'],z['t']))

def ob_eligible_through_retest(zone, candles, ready_ts, stream=None):
    """Preserve first geometric retest; never infer fill or erase it later.

    The live map still deletes on the crossing wick. An already observed
    touch and a chart deletion on the same closed bar are separate facts.
    Optional native queue record enforces capacity before the observation.
    """
    removed=stream.get('removed_ts') if stream else None
    for c in candles:
        if c['t'] < zone['break_ts']:
            continue
        if removed is not None and c['t']>removed:
            return False
        touch=c['t']>=ready_ts and c['h']>=zone['bottom'] and c['l']<=zone['top']
        if touch:
            return True
        if removed is not None and c['t']==removed:
            return False
        if ob_invalidated(zone,c):
            return False
    return removed is None

print('[KAIROS_LUX_OB] installed=true mitigation=HIGH_LOW capacity=100 active_age_expiry=false closed_native_inputs=true')
