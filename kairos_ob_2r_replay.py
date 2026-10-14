"""Read-only counterfactual: current M5 entries, frozen OB stop, TP=2R.

No sender, database, order execution or mutation of the live decision layer.
Fill is simulated at the already-armed proximal. M1 OHLC never proves live fill.
"""
import bisect
import hashlib
import json
import math
import threading
import time
from collections import Counter

LIVE_SPECS={'MN':('M',3650),'W1':('W',901),'D1':('D',261),'H4':('240',121),
            'H1':('60',36),'M30':('30',19),'M15':('15',10),'M5':('5',5),'M1':('1',2)}
DURATIONS={'M':2592000000,'W':604800000,'D':86400000,'240':14400000,
           '60':3600000,'30':1800000,'15':900000,'5':300000,'1':60000}
PAIRS={'BTCUSD','ETHUSD','SOLUSD','XRPUSD','LINKUSD','ADAUSD','AVAXUSD',
       'BNBUSD','AAVEUSD','NEARUSD','PENDLEUSD','INJUSD','ONDOUSD'}
_CACHE={'status':'IDLE','result':None,'error':None}
_LOCK=threading.Lock()
_EXPORT_CACHE={'status':'IDLE','result':None,'error':None}


def ob_plan(event, candles, engine, buffer_mult=0.25):
    if event.get('tf')!='M5' or event.get('zone_type') not in ('OB_bullish','OB_bearish'):
        return None
    direction=event.get('direction')
    if direction not in ('LONG','SHORT'):
        return None
    armed=event['timestamp']
    known=[c for c in candles if c['t']+300000 <= armed]
    atr=next((x for x in reversed(engine.compute_atr(known,14)) if x is not None),None)
    if atr is None or not math.isfinite(atr) or atr<=0:
        return None
    top=float(event['top']); bottom=float(event['bottom'])
    entry=top if direction=='LONG' else bottom
    sl=bottom-buffer_mult*atr if direction=='LONG' else top+buffer_mult*atr
    risk=abs(entry-sl); sign=1. if direction=='LONG' else -1.
    if not all(math.isfinite(x) for x in (entry,sl,risk,top,bottom)) or risk<=0 or top<bottom:
        return None
    return {'key':event.get('setup_key') or event['key'],'direction':direction,
            'entry':entry,'sl':sl,'risk':risk,'tp_2r':entry+sign*2*risk,
            'be_trigger':entry+sign*risk,'armed_ts':armed,'ready_ts':event['ready_ts'],
            'bottom':bottom,'top':top,'atr14':atr,'atr_buffer_mult':buffer_mult,
            'buffer_abs':buffer_mult*atr,'atr_last_closed_m5_open_ts':known[-1]['t'],
            'origin_kind':event.get('origin_kind') or 'STRUCTURAL_LIQUIDITY_CAPTURE',
            'htf_location':event.get('htf_location'),
            'capture_tf':event.get('capture_tf'),'capture_level':event.get('capture_level'),
            'major_confirm_ts':event.get('major_confirm_ts'),
            'internal_confirm_ts':event.get('internal_confirm_ts'),'poi':event.get('poi')}


def resolve_trade(plan, retest, m1, cutoff, be=False):
    def result(outcome, candle=None, r=None, **extra):
        return {'outcome':outcome,'R':r,'resolution_open_ts':candle['t'] if candle else None,
                'resolution_close_ts':candle['t']+60000 if candle else None,
                'resolution_candle_m1':candle,'be_at_1r':be,**extra}
    opened=retest['t']
    if plan['armed_ts']>opened:
        return result('NOT_ARMED_BEFORE_RETEST')
    bars=[c for c in m1 if opened<=c['t'] and c['t']+60000<=cutoff]
    if not bars or bars[0]['t']!=opened:
        return result('DATA_GAP')
    entry=plan['entry']; short=plan['direction']=='SHORT'
    filled=False; active_be=False; fill=None; previous=None
    for c in bars:
        if previous is not None and c['t']!=previous+60000:
            return result('DATA_GAP',c,simulated_fill_minute=fill)
        previous=c['t']
        if not filled:
            if c['t']>=opened+300000:
                return result('M1_RETEST_NOT_REPRODUCED')
            if not (c['l']<=entry<=c['h']):
                continue
            filled=True; fill=dict(c)
            # The OHLC does not order an exit relative to first proximal touch.
            hit_tp=c['l']<=plan['tp_2r'] if short else c['h']>=plan['tp_2r']
            hit_sl=c['h']>=plan['sl'] if short else c['l']<=plan['sl']
            hit_1r=c['l']<=plan['be_trigger'] if short else c['h']>=plan['be_trigger']
            if hit_tp or hit_sl or (be and hit_1r):
                return result('AMBIGUOUS_ENTRY_MINUTE',c,simulated_fill_minute=fill)
            continue
        hit_tp=c['l']<=plan['tp_2r'] if short else c['h']>=plan['tp_2r']
        stop=entry if active_be else plan['sl']
        hit_stop=c['h']>=stop if short else c['l']<=stop
        hit_1r=c['l']<=plan['be_trigger'] if short else c['h']>=plan['be_trigger']
        if hit_tp and hit_stop:
            return result('AMBIGUOUS',c,simulated_fill_minute=fill)
        if be and not active_be and hit_1r:
            touch_entry=c['h']>=entry if short else c['l']<=entry
            if touch_entry:
                return result('AMBIGUOUS_BE_ACTIVATION',c,simulated_fill_minute=fill)
            if hit_tp:
                return result('TP_2R',c,2.,simulated_fill_minute=fill)
            active_be=True
            continue
        if hit_stop:
            return result('BE' if active_be else 'SL',c,0. if active_be else -1.,simulated_fill_minute=fill)
        if hit_tp:
            return result('TP_2R',c,2.,simulated_fill_minute=fill)
    if previous+60000 < cutoff//60000*60000:
        return result('DATA_GAP',simulated_fill_minute=fill)
    return result('PENDING' if filled else 'M1_RETEST_NOT_REPRODUCED',
                  simulated_fill_minute=fill,be_active_at_cutoff=active_be)


def summarize(trades, field):
    outcomes=Counter(t[field]['outcome'] for t in trades)
    resolved=[t[field]['R'] for t in trades if t[field]['R'] is not None]
    binary=outcomes['TP_2R']+outcomes['SL']
    return {'entries':len(trades),'TP_2R':outcomes['TP_2R'],'SL':outcomes['SL'],
            'BE':outcomes['BE'],'PENDING':outcomes['PENDING'],
            'AMBIGUOUS':sum(v for k,v in outcomes.items() if k.startswith('AMBIGUOUS')),
            'excluded_data_or_timing':sum(v for k,v in outcomes.items() if k in
                ('DATA_GAP','NOT_ARMED_BEFORE_RETEST','M1_RETEST_NOT_REPRODUCED')),
            'all_outcomes':dict(outcomes),'resolved_only_net_R':sum(resolved),
            'resolved_only_expectancy_R':sum(resolved)/len(resolved) if resolved else None,
            'binary_win_rate_pct':100*outcomes['TP_2R']/binary if binary else None,
            'fees_slippage_included':False}


def fetch_inputs(engine, pair, start, end):
    raw={}; audit={}; symbol=engine._kairos_normalize_instrument(pair)
    for tf,(iv,days) in LIVE_SPECS.items():
        audit[tf]=[]
        # Fetch once with enough extra history to reproduce nominal rolling
        # native Bybit pages at every historical cutoff (not a moving fetch).
        pages=math.ceil((int(days*86400000/DURATIONS[iv])+20)/1000)
        bars=pages*1000+math.ceil((end-start)/DURATIONS[iv])+1
        raw[tf]=engine._fetch_bybit_klines_historico(symbol,iv,bars*DURATIONS[iv]/86400000,
                                                   fim_ts_ms=end,audit_metrics=audit[tf])
        if (not raw[tf] or any(x.get('error') or x.get('retCode')!=0 for x in audit[tf])):
            raise ValueError('INCOMPLETE_BYBIT_DATA:'+tf)
        raw[tf],_=engine._deduplicar_e_ordenar_candles(raw[tf])
    return raw,audit


def replay_from_candles(engine, pair, raw, start, end, buffer_mult=0.25):
    if end<=start or start%300000 or end%300000:
        raise ValueError('Window must use closed M5 boundaries')
    state={}; plans={}; observations=[]; excluded=[]; reasons=Counter(); gaps=[]
    closed={tf:engine._kairos_candles_fechados_ate(cs,LIVE_SPECS[tf][0],end) for tf,cs in raw.items()}
    closes={tf:[engine._kairos_candle_close_ts(c['t'],LIVE_SPECS[tf][0]) for c in cs]
            for tf,cs in closed.items()}
    all_m5=closed.get('M5',[]); all_m1=closed.get('M1',[])
    m1_times=[c['t'] for c in all_m1]
    duration=end-start; cycles=duration//300000
    last_setup=None
    for count,cutoff in enumerate(range(start,end+1,300000)):
        tf_map={}
        for tf,(iv,days) in LIVE_SPECS.items():
            prefix=closed.get(tf,[])[:bisect.bisect_right(closes.get(tf,[]),cutoff)]
            pages=math.ceil((int(days*86400000/DURATIONS[iv])+20)/1000)
            tf_map[tf]=prefix[-(pages*1000-1):]
        stale=engine._kairos_snapshot_freshness(tf_map,cutoff)
        if stale:
            gaps.append({'cutoff':cutoff,'reason':'STALE_NATIVE_CANDLES','tfs':stale})
            continue
        if len(tf_map['M15'])<40 or len(tf_map['M5'])<80:
            gaps.append({'cutoff':cutoff,'reason':'INSUFFICIENT_M15_M5'});continue
        # Gaps in price history can change structure or invent a first retest.
        missing=[tf for tf,(iv,_) in LIVE_SPECS.items() if any(
            b['t']!=engine._kairos_candle_close_ts(a['t'],iv)
            for a,b in zip(tf_map[tf],tf_map[tf][1:]))]
        if missing:
            gaps.append({'cutoff':cutoff,'reason':'STRUCTURE_DATA_GAP','tfs':missing});continue
        decision=engine.avaliar_vortex_decision_layer_v2(
            tf_map['M15'],tf_map['M5'],tf_map['D1'],candles_por_tf=tf_map,
            audit_pair=pair+':OB_2R_REPLAY',experimental_poi_policy='A_CURRENT',
            experimental_poi_state={},cutoff_ts=cutoff,audit_entries_only=True)
        last_setup=decision.get('entry_setup') or {}
        reasons[decision.get('failure_reason') or 'NONE']+=1
        events=[]
        engine._kairos_apply_entry_phase(state,decision,cutoff,start,events)
        for event in events:
            key=event['setup_key']
            if event['phase']=='ARMED':
                plan=ob_plan(event,tf_map['M5'],engine,buffer_mult)
                if plan:
                    plans.setdefault(key,plan)
                continue
            if event['phase']!='RETEST_OBSERVED':
                continue
            plan=plans.get(key)
            if (not plan or not event.get('armed_before_retest') or
                    not decision.get('entry_observation_confirmed')):
                excluded.append({'key':key,'timestamp':event['timestamp'],
                                 'reason':'NON_OB_OR_NOT_PROSPECTIVELY_ARMED_M5'});continue
            retest=last_setup.get('retest')
            if not retest or retest['t']<start:
                continue
            chain=decision.get('entry_audit_evidence') or {}
            # Export compact upstream evidence without fabricating a liquidity event.
            origin=({'kind':'HTF_FVG_TOUCH',**(chain.get('origin') or {})}
                    if chain.get('origin') else {'kind':'STRUCTURAL_LIQUIDITY_CAPTURE',**(chain.get('capture') or {})})
            m1=all_m1[bisect.bisect_left(m1_times,retest['t']):]
            observations.append({'plan':plan,'retest_m5':dict(retest),
                                 'retest_confirm_close_ts':event['timestamp'],
                                 'origin':origin,'intent':chain.get('intent'),
                                 'poi_shadow_audit':decision.get('poi_shadow_audit'),
                                 'fixed_2r':resolve_trade(plan,retest,m1,end),
                                 'be_1r_2r':resolve_trade(plan,retest,m1,end,be=True)})
        if count and count%48==0:
            print(f'[KAIROS_OB_2R_REPLAY] pair={pair} progress={count}/{cycles} entries={len(observations)}',flush=True)
    by_origin={}
    for origin in ('STRUCTURAL_LIQUIDITY_CAPTURE','HTF_FVG_TOUCH'):
        subset=[x for x in observations if x['plan']['origin_kind']==origin]
        by_origin[origin]={'fixed_2r':summarize(subset,'fixed_2r'),'be_1r_2r':summarize(subset,'be_1r_2r')}
    data_issue=any(x[field]['outcome'] in ('DATA_GAP','M1_RETEST_NOT_REPRODUCED')
                   for x in observations for field in ('fixed_2r','be_1r_2r'))
    return {'pair':pair,'window_start_ts':start,'window_end_ts':end,'expected_cycles':cycles,
            'bootstrap_evaluation':True,'evaluated_cycles':sum(reasons.values()),
            'coverage_complete':not gaps and not data_issue,'coverage_issues':gaps,
            'armed_ob_setups':len(plans),'retest_ob_entries':len(observations),
            'last_pending_ob_setup_key':last_setup.get('key') if last_setup and not last_setup.get('retest') and last_setup.get('key') in plans else None,
            'fixed_2r':summarize(observations,'fixed_2r'),'be_1r_2r':summarize(observations,'be_1r_2r'),
            'by_origin':by_origin,'trades':observations,'exclusions':excluded,'engine_failure_counts':dict(reasons),
            'input_fingerprint_sha256':hashlib.sha256(json.dumps(raw,sort_keys=True,separators=(',',':')).encode()).hexdigest(),
            'input_counts':{tf:len(cs) for tf,cs in raw.items()},
            'method':{'source':'CURRENT_ENTRY_ONLY_M5_ENGINE_WITH_CONTINUATION',
                      'stop':'OUTSIDE_OB_PLUS_ATR14_BUFFER_FROZEN_AT_ARMING','buffer_mult':buffer_mult,
                      'target_R':2.,'resolution_tf':'M1','fill':'SIMULATED_ARMED_PROXIMAL_TOUCH_NOT_LIVE_FILL',
                      'overlapping_positions':'INDEPENDENT_SETUP_SCENARIOS','fees_slippage':False,
                      'production_math_changed':False,'database_writes':False,'telegram_sends':False,
                      'limits':'Historical reproduction; current OHLC may differ from first publication. Partial coverage is not a complete backtest.'}}


def native_export(engine, pair, end, include_fvg=False):
    """Bounded native OHLC export; never evaluates or records a trade."""
    raw,audit=fetch_inputs(engine,pair,end-86400000,end)
    candles={tf:engine._kairos_candles_fechados_ate(cs,LIVE_SPECS[tf][0],end)
             for tf,cs in raw.items()}
    result={'pair':pair,'instrument':engine._kairos_normalize_instrument(pair),
            'cutoff_ts':end,'candles':candles,'fetch_audit':audit,
            'sha256':hashlib.sha256(json.dumps(candles,sort_keys=True,separators=(',',':')).encode()).hexdigest(),
            'read_only':True,'fill_assumed':False,'evaluation_performed':False,
            'note':'Historical OHLC export, not proof of first-publication data or trade execution.'}
    if include_fvg:
        from kairos_calendar_map import closed_calendar_years
        annual=closed_calendar_years(candles.get('MN') or [],end)
        mapped={**candles,'Y1':annual}
        result['fvg_states']={tf:engine._kairos_fvg_states(cs) for tf,cs in mapped.items()}
        result['ob_states']={tf:engine._kairos_order_blocks_map(cs) for tf,cs in mapped.items()}
        result['calendar_year_map']={'tf':'Y1','role':'CONTEXT_ONLY','calendar':'UTC',
            'candles':annual,'structure_major':engine.compute_lux_structure_events(annual,50),
            'structure_internal':engine.compute_lux_internal_structure(annual,5),
            'coverage':{'closed_years':len(annual),'major_lookback':50,'internal_lookback':5,
                        'atr_period':200,'partial_year_used':False},
            'note':'Only contiguous complete 12-month years. No new signal authorization.'}
        result['fvg_math_performed']=True
        result['fvg_reference']='LUX_SMC_V6_AND_LUX_IFVG_V6'
    return result


def register(app, engine):
    from flask import jsonify, request
    def job(pair, start, end, buffer_mult):
        try:
            raw,audit=fetch_inputs(engine,pair,start,end)
            result=replay_from_candles(engine,pair,raw,start,end,buffer_mult)
            result['fetch_audit']=audit
            _CACHE.update(status='DONE',result=result,error=None,finished_at=int(time.time()*1000))
            print(f'[KAIROS_OB_2R_REPLAY] pair={pair} DONE coverage_complete={result["coverage_complete"]} fixed={result["fixed_2r"]}',flush=True)
        except Exception as exc:
            _CACHE.update(status='ERROR',error=str(exc),result=None,finished_at=int(time.time()*1000))
            print('[KAIROS_OB_2R_REPLAY] ERROR '+str(exc),flush=True)
        finally:
            _LOCK.release()
    @app.route('/experiment/ob_2r_replay',methods=['GET'])
    def ob_2r_replay_endpoint():
        if request.args.get('start')=='1':
            try:
                pair=request.args.get('pair','NEARUSD').upper()
                days=int(request.args.get('dias','1'))
                end=int(request.args.get('fim_ts_ms') or int(time.time()*1000))//300000*300000
                mult=float(request.args.get('buffer_mult','0.25'))
                if pair not in PAIRS or days!=1 or end>int(time.time()*1000) or not math.isfinite(mult) or not 0<mult<=1:
                    raise ValueError('Supported pair, dias=1, past cutoff and buffer_mult in (0,1] required')
            except (TypeError,ValueError) as exc:
                return jsonify({'error':str(exc)}),400
            if not _LOCK.acquire(blocking=False):
                return jsonify({'status':'RUNNING','started':False}),409
            start=end-86400000
            _CACHE.update(status='RUNNING',result=None,error=None,pair=pair,start_ts=start,end_ts=end,
                          started_at=int(time.time()*1000),finished_at=None)
            threading.Thread(target=job,args=(pair,start,end,mult),daemon=True).start()
            return jsonify({'status':'STARTING','pair':pair,'read_only':True,'start_ts':start,'end_ts':end}),202
        return jsonify(_CACHE)
    def export_job(pair,end,include_fvg=False):
        try:
            result=native_export(engine,pair,end,include_fvg=include_fvg)
            _EXPORT_CACHE.update(status='DONE',result=result,error=None)
            print(f'[KAIROS_NATIVE_EXPORT] DONE pair={pair} cutoff={end}',flush=True)
        except Exception as exc:
            _EXPORT_CACHE.update(status='ERROR',result=None,error=str(exc))
            print('[KAIROS_NATIVE_EXPORT] ERROR '+str(exc),flush=True)
        finally:
            _LOCK.release()
    @app.route('/experiment/native_candles',methods=['GET'])
    def native_candles_endpoint():
        if request.args.get('start')=='1':
            try:
                include_fvg=request.args.get('include_fvg','0')
                if include_fvg not in ('0','1'):raise ValueError('include_fvg must be 0 or 1')
                pair=request.args.get('pair','AVAXUSD').upper()
                end=int(request.args.get('fim_ts_ms') or int(time.time()*1000))
                if pair not in PAIRS or not 1577836800000<=end<=int(time.time()*1000):
                    raise ValueError('Supported pair and cutoff between 2020 and now required')
                end=end//300000*300000
            except (TypeError,ValueError) as exc:
                return jsonify({'error':str(exc)}),400
            if not _LOCK.acquire(blocking=False):
                return jsonify({'status':'BUSY','started':False}),409
            _EXPORT_CACHE.update(status='RUNNING',result=None,error=None,pair=pair,cutoff_ts=end)
            threading.Thread(target=export_job,args=(pair,end,include_fvg=='1'),daemon=True).start()
            return jsonify({'status':'STARTING','pair':pair,'read_only':True}),202
        return jsonify(_EXPORT_CACHE)
    print('[KAIROS_OB_2R_REPLAY] audit_route_installed=true live_math_changed=false',flush=True)
