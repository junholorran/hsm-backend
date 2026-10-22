"""Read-only presentation of native Kairos maps; never authorizes an entry."""
import copy
import hashlib
import json
import math

DURATIONS={'H4':14400000,'H1':3600000}


def finite(v):
    return isinstance(v,(float,int)) and math.isfinite(v)


def enrich_report(report, tf_map, decision):
    out=copy.deepcopy(report);cut=out['asof_ts']
    source=decision.get('context_map_source') or {}
    out['zones']=[]
    if source.get('asof_ts')!=cut:
        out['status']='CONTEXT_MAP_TIME_MISMATCH'
        return out
    closed=[c for c in tf_map.get('M5',[]) if c.get('t') is not None and c['t']+300000<=cut and finite(c.get('c'))]
    price=max(closed,key=lambda c:c['t']) if closed else None
    if price is None or price['t']+600000<=cut:
        out['status']='MISSING_FRESH_M5_PRICE'
        return out
    px=price['c']
    captures=(decision.get('structural_sweep_audit') or {}).get('candidates') or []
    captures=[c for c in captures if finite(c.get('capture_confirm_ts')) and c['capture_confirm_ts']<=cut]
    identities={(c.get('liquidity_tf'),c.get('liquidity_type'),c.get('liquidity_origin_ts'),c.get('nivel')):c for c in captures}
    for row in out['levels']:
        evidence=identities.get((row.get('tf'),row.get('type'),row.get('origin_ts'),row.get('level')))
        if evidence and row['state']=='ACTIVE':
            row.update(native_state='ACTIVE',state='CAPTURED',captured_tf='M15',
                       captured_ts=evidence['first_capture_ts'],capture_confirm_ts=evidence['capture_confirm_ts'])
    out['current_price']={'value':px,'close_ts':price['t']+300000,'reference':'LAST_CLOSED_M5_CLOSE'}
    active=[r for r in out['levels'] if r['state']=='ACTIVE']
    out['nearest_available']={
        'above':min((r for r in active if r['level']>px),key=lambda r:r['level'],default=None),
        'below':max((r for r in active if r['level']<px),key=lambda r:r['level'],default=None),
        'reference':'LAST_CLOSED_M5_CLOSE','predicted_destination':None}
    bias=out['daily']['bias'];wanted={'BULLISH':'alta','BEARISH':'baixa'}.get(bias)
    for tf,duration in DURATIONS.items():
        for original in source.get('zones',{}).get(tf,[]):
            z=copy.deepcopy(original)
            born=z.get('flip_ts') if z.get('state')=='IFVG' else z.get('break_ts',z.get('created_ts'))
            if (z.get('state') not in ('ATIVA','TOCADA','PARCIAL','IFVG')
                    or not finite(born) or born+duration>cut
                    or not finite(z.get('bottom')) or not finite(z.get('top'))
                    or not 0<z['bottom']<=z['top']):
                continue
            role=('ZONA_PARA_OBSERVAR_RECUO' if wanted and z.get('direcao')==wanted
                  and (z['bottom']<=px if wanted=='alta' else z['top']>=px) else 'CONTEXTO')
            out['zones'].append({'tf':tf,'type':z.get('tipo'),'direction':z.get('direcao'),
                'bottom':z['bottom'],'top':z['top'],'state':z['state'],'available_ts':born+duration,
                'id':z.get('id'),'origin_ts':z.get('origin_ts',z.get('t')),
                'role':role,'inside':z['bottom']<=px<=z['top']})
    out['zones'].sort(key=lambda z:(z['tf'],z['bottom'],str(z['type'])))
    capture=max(captures,key=lambda c:(c.get('first_capture_ts',0),{'W1':4,'D1':3,'H4':2,'H1':1}.get(c.get('liquidity_tf'),0)),default=None)
    out['latest_capture']=copy.deepcopy(capture)
    setup=decision.get('entry_setup') or {}
    setup_known=finite(setup.get('ready_ts')) and setup['ready_ts']<=cut
    reason=decision.get('failure_reason')
    invalidation=(setup.get('poi') or {}).get('m15_invalidation_close_ts')
    past_retest=(setup.get('retest') or {}).get('t')
    invalidated=setup_known and finite(invalidation) and invalidation<=cut
    historical_retest=setup_known and finite(past_retest) and past_retest+300000<=cut
    if invalidated:
        stage='SETUP_ANTERIOR_INVALIDADO'
    elif setup_known and decision.get('entry_observation_confirmed') and finite(decision.get('retest_confirm_close_ts')) and decision['retest_confirm_close_ts']<=cut:
        stage='RETESTE_M5_OBSERVADO'
    elif historical_retest:
        stage='RETESTE_HISTORICO_SEM_NOVA_ENTRADA'
    elif setup_known and setup.get('tf')=='M5':
        stage='AGUARDANDO_RETESTE_M5'
    elif decision.get('intent_m15_found'):
        stage='AGUARDANDO_POI_CAUSAL_OU_REFINAMENTO_M5'
    elif capture or decision.get('htf_location') or reason in ('SEM_INTENCAO_M15_APOS_CAPTURA_ESTRUTURAL','SEM_INTENCAO_M15_APOS_FVG_HTF'):
        stage='AGUARDANDO_CONFIRMACAO_M15'
    else:
        stage='AGUARDANDO_ZONA_OU_CAPTURA'
    # A detector observation is not mislabeled as motor authorization.
    out['stage']={'code':stage,'engine_reason':reason,'direction':decision.get('direction') if setup_known and not invalidated and not historical_retest else None,
                  'setup':copy.deepcopy(setup) if setup_known and not invalidated and not historical_retest else None,'entry_authorized':False}
    out['m15_observation']=None
    rows=tf_map.get('M15') or []
    if rows:
        from kairos_ludo_structure import structure_snapshot
        snap=structure_snapshot(rows,900000,cut)
        if snap['status']=='OK' and snap['events']:
            ev=snap['events'][-1]
            out['m15_observation']={k:ev[k] for k in ('kind','side','level','close_ts','close')}
    daily=out['daily'];prev=daily.get('previous_day') or {}
    review=(daily.get('reason')=='CLOSE_ABOVE_PREVIOUS_HIGH' and px<=prev.get('h',px-1)
            or daily.get('reason')=='CLOSE_BELOW_PREVIOUS_LOW' and px>=prev.get('l',px+1)
            or daily.get('reason')=='HIGH_CAPTURE_AND_CLOSE_INSIDE' and px>=prev.get('h',px+1)
            or daily.get('reason')=='LOW_CAPTURE_AND_CLOSE_INSIDE' and px<=prev.get('l',px-1))
    out['daily_review_required']=bool(review)
    # One full combined map per actual stage/capture/review transition, never
    # a new message merely because a market price or snapshot time changes.
    cap_key=({k:capture.get(k) for k in ('liquidity_tf','liquidity_type','nivel','first_capture_ts','post_capture_state','reaction_confirm_ts')} if capture else None)
    key=[daily.get('available_ts'),stage,setup.get('key') if setup_known else None,cap_key,bool(review),out['m15_observation']]
    out['delivery_key']=hashlib.sha256(json.dumps(key,sort_keys=True).encode()).hexdigest()[:20]
    out['entry_authorized']=False
    return out
