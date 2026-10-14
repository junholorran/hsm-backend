"""LuxAlgo SMC v6 displayStructure(internal=true), default confluence=false.

Reference: https://www.luxalgo.com/library/indicator/smart-money-concepts-smc/
Requires a distinct confirmed external pivot and actual close crossover.
"""

def internal_structure_events(candles, size=5, external_size=50):
    levels={'high':None,'low':None};outer={'high':None,'low':None}
    origin={'high':None,'low':None};crossed={'high':False,'low':False}
    leg=0;outer_leg=0;bias=None;out=[]
    for i,c in enumerate(candles):
        previous=dict(levels)
        if i>=external_size:
            candidate=candles[i-external_size];right=candles[i-external_size+1:i+1]
            new=0 if candidate['h']>max(q['h'] for q in right) else 1 if candidate['l']<min(q['l'] for q in right) else outer_leg
            if new!=outer_leg:
                side='low' if new else 'high';outer[side]=candidate['l' if new else 'h']
            outer_leg=new
        if i>=size:
            candidate=candles[i-size];right=candles[i-size+1:i+1]
            new=0 if candidate['h']>max(q['h'] for q in right) else 1 if candidate['l']<min(q['l'] for q in right) else leg
            if new!=leg:
                side='low' if new else 'high'
                levels[side]=candidate['l' if new else 'h'];origin[side]=candidate['t'];crossed[side]=False
            leg=new
        if not i:
            continue
        for side,direction,protected in (('high','alta','low'),('low','baixa','high')):
            level=levels[side];old=previous[side];external=outer[side]
            # Pine comparisons with na are false; internal != external is required.
            if level is None or old is None or external is None or level==external or crossed[side]:
                continue
            cross=(c['c']>level and candles[i-1]['c']<=old) if side=='high' else (c['c']<level and candles[i-1]['c']>=old)
            if not cross:
                continue
            out.append(dict(tipo='CHoCH' if bias is not None and bias!=direction else 'BOS',
                direcao=direction,nivel=level,broken_swing_origin_ts=origin[side],
                protected_swing_type=protected.upper(),protected_swing_level=levels[protected],
                protected_swing_origin_ts=origin[protected],leg_start_ts=origin[protected],
                leg_end_ts=c['t'],t=c['t'],index=i))
            crossed[side]=True;bias=direction
    return out

print('[KAIROS_LUX_INTERNAL] installed=true internal=5 external=50 distinct_level=true close_crossover=true')
