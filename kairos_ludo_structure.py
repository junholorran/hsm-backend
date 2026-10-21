# SPDX-License-Identifier: MPL-2.0
# Structure algorithm derived from SMC Structures and FVG, copyright LudoGH68.
# Closed-bar Python port of the structure block supplied by the user.
"""Parallel structure evidence. Never authorizes entries or changes the live map.

Pine's function-local `var idx`, loop overwrite order, tie handling and initial
state are preserved. Rolling history can have a different initial state than
TradingView; parity is a measured result, not a claim for every chart history.
"""
import copy
import threading

DURATIONS={'M5':300000,'M15':900000,'M30':1800000,'H1':3600000,
           'H4':14400000,'D1':86400000,'W1':604800000}


def structure_snapshot(candles, duration, cutoff):
    """Run the supplied body-break structure on closed, contiguous native bars."""
    cs=[c for c in candles if c['t']+duration<=cutoff]
    if not cs:
        return {'status':'NO_CLOSED_CANDLES','state':None,'events':[]}
    if any(b['t']-a['t']!=duration for a,b in zip(cs,cs[1:])):
        return {'status':'DATA_GAP','state':None,'events':[]}
    high=cs[0]['h']; low=cs[0]['l']; high_start=low_start=direction=0
    high_idx=low_idx=0; events=[]; low_known=high_known=cs[0]['t']+duration
    for n,c in enumerate(cs):
        # ta.highestbars/lowestbars: most recent occurrence wins equal extrema.
        length=10 if n>10 else n+1
        max_bar=-max(range(length),key=lambda k:(cs[n-k]['h'],-k))
        min_bar=-min(range(length),key=lambda k:(cs[n-k]['l'],k))
        for offset in range(10):
            if n-offset-2<0:
                continue  # Pine comparisons with na do not enter the branch.
            older,mid,new=cs[n-offset-2],cs[n-offset-1],cs[n-offset]
            if mid['h']>older['h'] and new['h']<=mid['h'] and -(offset+1)>=max_bar:
                high_idx=-(offset+1)
            if mid['l']<older['l'] and new['l']>=mid['l'] and -(offset+1)>=min_bar:
                low_idx=-(offset+1)
        if high_idx==0: high_idx=max_bar
        if low_idx==0: low_idx=min_bar
        low_broken=(n>=3 and c['c']<low and all(cs[n-j]['c']>=low and n-j>low_start for j in (1,2,3))) or (direction==2 and c['c']<low)
        high_broken=(n>=3 and c['c']>high and all(cs[n-j]['c']<=high and n-j>high_start for j in (1,2,3))) or (direction==1 and c['c']>high)
        closed=c['t']+duration
        if low_broken:
            events.append({'kind':'BOS' if direction==1 else 'CHoCH','side':'DOWN',
                'level':low,'origin_ts':cs[low_start]['t'],'level_known_ts':low_known,
                'open_ts':c['t'],'close_ts':closed,'close':c['c'],'index':n,
                'opposite_level':high,'opposite_origin_ts':cs[high_start]['t']})
            direction=1; high_start=n+high_idx; low_start=n
            high=cs[high_start]['h']; low=c['l']; low_known=high_known=closed
        elif high_broken:
            events.append({'kind':'BOS' if direction==2 else 'CHoCH','side':'UP',
                'level':high,'origin_ts':cs[high_start]['t'],'level_known_ts':high_known,
                'open_ts':c['t'],'close_ts':closed,'close':c['c'],'index':n,
                'opposite_level':low,'opposite_origin_ts':cs[low_start]['t']})
            direction=2; high_start=n; low_start=n+low_idx
            high=c['h']; low=cs[low_start]['l']; low_known=high_known=closed
        else:
            if c['h']>high and direction in (0,2):
                if not all(n-j>high_start for j in (1,2,3)):
                    high=c['h']; high_start=n; high_known=closed
            elif c['l']<low and direction in (0,1):
                if not all(n-j>low_start for j in (1,2,3)):
                    low=c['l']; low_start=n; low_known=closed
    return {'status':'OK','state':{'high':high,'low':low,
        'high_origin_ts':cs[high_start]['t'],'low_origin_ts':cs[low_start]['t'],
        'high_known_ts':high_known,'low_known_ts':low_known,
        'direction':{0:'UNSET',1:'DOWN',2:'UP'}[direction],
        'last_closed_ts':cs[-1]['t']+duration,'history_start_ts':cs[0]['t'],
        'history_count':len(cs)},'events':events}


def observe(tf_map, cutoff, decision):
    """Observe the first M5 reversal after a known HTF capture and reclaim.

    This is evidence of structure, not proof of displacement, a causal order
    block, a prospective retest, or a fill. Those entry gates remain unapproved
    by this observer. Existing capture/map selection and outputs are untouched.
    """
    snapshots={tf:structure_snapshot(tf_map.get(tf) or [],duration,cutoff)
               for tf,duration in DURATIONS.items() if tf_map.get(tf)}
    m5=[c for c in tf_map.get('M5',[]) if c['t']+300000<=cutoff]
    candidates=(decision.get('structural_sweep_audit') or {}).get('candidates') or []
    captures=[c for c in candidates if c.get('liquidity_tf') in ('W1','D1','H4','H1')
        and c.get('native_capture_confirm_ts') is not None
        and c['native_capture_confirm_ts']<=cutoff
        and c.get('liquidity_side') in ('HIGH','LOW') and c.get('nivel') is not None]
    capture=max(captures,key=lambda c:(c.get('first_capture_ts',0),DURATIONS.get(c.get('liquidity_tf'),0))) if captures else None
    candidate=None
    if capture and snapshots.get('M5',{}).get('status')=='OK':
        level=capture['nivel']; high_side=capture['liquidity_side']=='HIGH'
        known=capture['native_capture_confirm_ts']
        direction='DOWN' if high_side else 'UP'
        for event in snapshots['M5']['events']:
            if event['side']!=direction or event['kind']!='CHoCH' or event['open_ts']<known:
                continue
            # Level was fixed before this break candle, never inferred later.
            if event['level_known_ts']>event['open_ts']:
                continue
            reactions=[c for c in m5 if known<=c['t'] and c['t']+300000<=event['open_ts']
                       and (c['c']<level if high_side else c['c']>level)]
            if not reactions:
                continue
            reaction=reactions[-1]
            candle=m5[event['index']]
            if not (candle['c']<level and candle['c']<candle['o'] if high_side
                    else candle['c']>level and candle['c']>candle['o']):
                continue
            candidate={'status':'BREAK_OBSERVED_NOT_AN_ENTRY','direction':'SHORT' if high_side else 'LONG',
                'capture':copy.deepcopy(capture),'reaction':{'open_ts':reaction['t'],
                'close_ts':reaction['t']+300000,'close':reaction['c']},'break':dict(event),
                'entry_authorized':False,'unverified_gates':['DISPLACEMENT','CAUSAL_POI','PROSPECTIVE_RETEST']}
            break
    setup=decision.get('entry_setup') or {}
    return {'mode':'SHADOW_ONLY','source':'LudoGH68 SMC Structures and FVG / supplied structure block',
        'body_break':True,'cutoff_ts':cutoff,'structures':{tf:{k:v for k,v in s.items() if k!='events'} for tf,s in snapshots.items()},
        'candidate':candidate,'legacy_setup_key':setup.get('key'),
        'legacy_direction':setup.get('direction'),'entry_authorized':False,
        'fill_assumed':False,'production_decision_changed':False}


_SHADOW_LATEST = {}
_SHADOW_LOCK = threading.RLock()


def record_shadow(pair, tf_map, cutoff, decision):
    """Cache one observation per closed cutoff, with bounded per-pair storage."""
    import json
    with _SHADOW_LOCK:
        previous = _SHADOW_LATEST.get(pair)
        if previous and previous['cutoff_ts'] >= cutoff:
            return copy.deepcopy(previous)
        result = observe(tf_map, cutoff, decision)
        _SHADOW_LATEST[pair] = copy.deepcopy(result)
        print('[KAIROS_LUDO_SHADOW] ' + json.dumps({'pair': pair, **result}, separators=(',', ':')), flush=True)
        return copy.deepcopy(result)


def latest_shadow():
    """A read-only copy; no evaluation, market fetch, or Telegram delivery."""
    with _SHADOW_LOCK:
        return copy.deepcopy(_SHADOW_LATEST)
