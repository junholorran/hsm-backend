"""Observation only: experimental FVG strength, never an entry gate.

Thresholds below are Kairos research hypotheses, NOT official ICT rules.
Native Ludo, liquidity, FVG lifecycle, plan state and delivery are untouched.
"""
import math
from statistics import median

VERSION = 'FVG_FORCE_SHADOW_V1'
THRESHOLDS = dict(body_fraction_min=0.60, closing_tail_max=0.25,
                  body_median_multiple_min=1.50, baseline_bars=20)


def _valid(c):
    return (all(isinstance(c.get(k), (int, float)) and not isinstance(c[k],bool)
                and math.isfinite(c[k]) for k in ('t','o','h','l','c'))
            and c['l'] <= min(c['o'],c['c']) <= max(c['o'],c['c']) <= c['h'])


def _metrics(rows, index, side, duration):
    c=rows[index]; span=c['h']-c['l']; body=abs(c['c']-c['o'])
    previous=rows[max(0,index-20):index]
    covered=(len(previous)==20 and all(b['t']-a['t']==duration
              for a,b in zip(previous+[c],(previous+[c])[1:])))
    baseline=median(abs(p['c']-p['o']) for p in previous) if covered else None
    baseline=baseline if baseline and baseline>0 else None
    fraction=body/span if span else None
    tail=((c['h']-c['c']) if side=='UP' else (c['c']-c['l']))/span if span else None
    expansion=body/baseline if baseline else None
    directional=c['c']>c['o'] if side=='UP' else c['c']<c['o']
    strong=(directional and fraction>=THRESHOLDS['body_fraction_min']
            and tail<=THRESHOLDS['closing_tail_max']
            and expansion>=THRESHOLDS['body_median_multiple_min']) if all(
                x is not None for x in (fraction,tail,expansion)) else None
    return dict(open_ts=c['t'],close_ts=c['t']+duration,body=body,
                body_fraction=fraction,closing_tail_fraction=tail,
                prior_body_median=baseline,body_median_multiple=expansion,
                baseline_complete=covered,experimental_strong=strong)


def assess_fvg(candles, zone, event, duration, cutoff):
    """Assess one native same-timeframe triple and Ludo event on closed data.

    Direct means B is the actual breaking candle. Leg means A begins after
    the event's protected opposite origin and C closes by the break. Neither
    is an inference that relevant liquidity was captured. No lifecycle check
    or trading permission is supplied by this diagnostic.
    """
    result=dict(version=VERSION,mode='OBSERVATION_ONLY',changes_entry_decision=False,
                thresholds=dict(THRESHOLDS),status='INVALID_INPUT')
    if duration<=0 or event.get('side') not in ('UP','DOWN'):
        return result
    sources=[zone.get(k) for k in ('source_a','source_mid','source_c')]
    if not all(isinstance(c,dict) and _valid(c) for c in sources):
        return dict(result,status='INVALID_CANDLES')
    a,b,c=sources
    if b['t']-a['t']!=duration or c['t']-b['t']!=duration:
        return dict(result,status='NONCONTIGUOUS_SOURCES')
    if c['t']+duration>cutoff or event.get('close_ts',math.inf)>cutoff:
        return dict(result,status='NOT_YET_CLOSED')
    rows=[r for r in candles if r['t']+duration<=cutoff]
    if not all(_valid(r) for r in rows) or any(y['t']<=x['t'] for x,y in zip(rows,rows[1:])):
        return dict(result,status='INVALID_CANDLES')
    positions={r['t']:i for i,r in enumerate(rows)}
    for source in sources:
        native=rows[positions[source['t']]] if source['t'] in positions else None
        if native is None or any(native[k]!=source[k] for k in ('o','h','l','c')):
            return dict(result,status='SOURCE_MISMATCH')
    bi=positions.get(event['close_ts']-duration)
    if bi is None:
        return dict(result,status='BREAK_NOT_IN_NATIVE_BARS')
    side=event['side']; breaking=rows[bi]
    if not isinstance(event.get('level'),(int,float)) or not math.isfinite(event['level']):
        return result
    beyond=breaking['c']-event['level'] if side=='UP' else event['level']-breaking['c']
    geometry=(c['l']>a['h']) if side=='UP' else (c['h']<a['l'])
    expected_type='FVG_bullish' if side=='UP' else 'FVG_bearish'
    bottom,top=(a['h'],c['l']) if side=='UP' else (c['h'],a['l'])
    bounds_match=all(isinstance(zone.get(k),(int,float)) and math.isclose(zone[k],v,abs_tol=1e-9)
                     for k,v in (('bottom',bottom),('top',top)))
    geometry=geometry and zone.get('tipo')==expected_type and bounds_match
    start=event.get('opposite_origin_ts')
    leg=(start is not None and start<=a['t'] and c['t']+duration<=event['close_ts'])
    direct=(b['t']+duration==event['close_ts'] and start is not None and start<=a['t'])
    leg=leg or direct
    mid=_metrics(rows,positions[b['t']],side,duration)
    break_metrics=_metrics(rows,bi,side,duration)
    candidate=(geometry and beyond>0 and direct and mid['experimental_strong'])
    reasons=[]
    if not geometry:reasons.append('NO_NATIVE_FVG_GEOMETRY')
    if beyond<=0:reasons.append('NO_CLOSE_BEYOND_STRUCTURE')
    if not direct:reasons.append('FVG_NOT_CENTERED_ON_BREAK_CANDLE')
    if mid['experimental_strong'] is None:reasons.append('STRENGTH_UNKNOWN')
    elif not mid['experimental_strong']:reasons.append('MIDDLE_NOT_EXPERIMENTALLY_STRONG')
    return dict(result,status='OBSERVED',geometry_valid=geometry,
                sources=[dict(s) for s in sources],direct_break=direct,
                inside_causal_leg=leg,break_close_ts=event['close_ts'],
                close_beyond_level=beyond,middle=mid,break_candle=break_metrics,
                experimental_candidate=candidate,reasons=reasons)


def _measure_selected(rows, zone, side, duration, cutoff):
    sources=[zone.get(k) for k in ('source_a','source_mid','source_c')]
    if not all(isinstance(c,dict) and _valid(c) for c in sources):
        return dict(status='INVALID_CANDLES')
    a,b,c=sources
    if c['t']+duration>cutoff:
        return dict(status='NOT_YET_CLOSED')
    if b['t']-a['t']!=duration or c['t']-b['t']!=duration:
        return dict(status='NONCONTIGUOUS_SOURCES')
    if not all(_valid(r) for r in rows) or any(y['t']<=x['t'] for x,y in zip(rows,rows[1:])):
        return dict(status='INVALID_CANDLES')
    positions={r['t']:i for i,r in enumerate(rows)}
    for source in sources:
        native=rows[positions[source['t']]] if source['t'] in positions else None
        if native is None or any(native[k]!=source[k] for k in ('o','h','l','c')):
            return dict(status='SOURCE_MISMATCH')
    geometry=(c['l']>a['h']) if side=='UP' else (c['h']<a['l'])
    bottom,top=(a['h'],c['l']) if side=='UP' else (c['h'],a['l'])
    expected='FVG_bullish' if side=='UP' else 'FVG_bearish'
    geometry=geometry and zone.get('tipo')==expected and all(
        isinstance(zone.get(k),(int,float)) and math.isclose(zone[k],v,abs_tol=1e-9)
        for k,v in (('bottom',bottom),('top',top)))
    return dict(status='MEASURED',sources=[dict(r) for r in sources],
                geometry_valid=geometry,bottom=bottom,top=top,
                middle=_metrics(rows,positions[b['t']],side,duration))


def observe_view(tf_map, view, cutoff):
    """Optional diagnostics: actual M15 break and M5 FVG are separate evidence."""
    base=dict(version=VERSION,mode='OBSERVATION_ONLY',changes_entry_decision=False,
              thresholds=dict(THRESHOLDS))
    event=view.get('m15_break');setup=view.get('setup') or {}
    if not event or event.get('side') not in ('UP','DOWN'):
        return dict(base,status='NO_STRUCTURAL_BREAK')
    m15=[r for r in tf_map.get('M15',[]) if r['t']+900000<=cutoff]
    parent=dict(status='NATIVE_BREAK_UNAVAILABLE')
    if all(_valid(r) for r in m15) and all(y['t']>x['t'] for x,y in zip(m15,m15[1:])):
        index=next((i for i,r in enumerate(m15) if r['t']+900000==event['close_ts']),None)
        if index is not None:
            candle=m15[index]
            if ((event.get('open_ts') is None or event['open_ts']==candle['t']) and
                (event.get('close') is None or event['close']==candle['c'])):
                parent=dict(status='MEASURED',candle=_metrics(m15,index,event['side'],900000),
                            source=dict(candle),level=event['level'])
    base['parent_m15_break']=parent
    if not setup.get('poi'):
        return dict(base,status='NO_SELECTED_FVG')
    from kairos_ludo_structure import structure_snapshot
    zone=setup['poi'];rows=[r for r in tf_map.get('M5',[]) if r['t']+300000<=cutoff]
    selected=_measure_selected(rows,zone,event['side'],300000,cutoff)
    base.update(selected_fvg=selected,zone_id=zone.get('id'),association_tf='M5')
    if selected['status']!='MEASURED':
        return dict(base,status=selected['status'])
    snap=structure_snapshot(rows,300000,cutoff)
    source_b=zone['source_mid']['t']
    native=next((e for e in reversed(snap['events']) if e['side']==event['side']
                 and e['close_ts']==source_b+300000),None)
    if native is None:
        return dict(base,status='NO_DIRECT_M5_BREAK')
    assessment=assess_fvg(rows,zone,native,300000,cutoff)
    return dict(base,**{k:v for k,v in assessment.items() if k not in base})
