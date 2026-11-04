import copy
import pytest
import kairos_capture_chains as chains
from kairos_notice_brief import plan_ref,format_event_group
from test_ict_plan_lifecycle import run_plan,D


def armed(monkeypatch,side='LONG'):
    monkeypatch.setenv('KAIROS_M15_PRIMARY_FVG','true')
    state={}
    for n in range(20,24):state,ev=run_plan(state,n*D,side)
    return state,next(e for e in ev if e['stage']=='M15_ARMADO')


def contrary(side='LONG',at=24):
    return dict(kind='CHoCH',side='DOWN' if side=='LONG' else 'UP',level=100.6 if side=='LONG' else 99.4,
                origin_ts=21*D,level_known_ts=23*D,open_ts=(at-1)*D,close_ts=at*D,
                close=100.4 if side=='LONG' else 99.6,opposite_level=101 if side=='LONG' else 99,
                opposite_origin_ts=22*D)


def reaction(side='LONG'):
    return dict(key='ACCEPTANCE:24',state='ACCEPTANCE_CONTINUATION',direction='SHORT' if side=='LONG' else 'LONG',
                confirmed_ts=24*D,leg_start_ts=23*D)


@pytest.mark.parametrize('side',['LONG','SHORT'])
@pytest.mark.parametrize('cause',['break','reaction','both'])
def test_armed_native_ludo_context_change_warns_without_replacing_or_cancelling(monkeypatch,side,cause):
    state,entry=armed(monkeypatch,side);before=copy.deepcopy(next(iter(state['captures'].values()))['plan'])
    kwargs=dict(extra_events=[contrary(side)] if cause!='reaction' else [],reaction_override=reaction(side) if cause!='break' else None)
    state,ev=run_plan(state,24*D,side,**kwargs)
    p=next(iter(state['captures'].values()))['plan']
    assert not p['invalidated']
    for k in ('break','parent_zone','m15_zone','invalidation','episode_key','armed_ts'):assert p[k]==before[k]
    notices=[e for e in ev if e['stage']=='PLANO_EM_ATENCAO']
    assert len(notices)==1
    assert len(notices[0]['context_reasons'])==(2 if cause=='both' else 1)
    assert plan_ref(notices[0])==plan_ref(entry)
    assert not any(e['stage'] in ('PLANO_INVALIDADO','TOQUE_E_INVALIDACAO','M15_QUEBRA','M15_ARMADO') for e in ev)
    text='\n'.join(format_event_group(notices,now_ts=24*D))
    assert 'PLANO EM ATENÇÃO' in text and 'Plano mantido' in text and 'Zona M15:' in text
    assert 'não é cancelamento' in text and plan_ref(entry) in text
    frozen=copy.deepcopy(state)
    state,ev=run_plan(state,24*D,side,**kwargs)
    assert state==frozen and ev==[]
    state,ev=run_plan(state,24*D+chains.M5,side,**kwargs)
    assert not any(e['stage']=='PLANO_EM_ATENCAO' for e in ev)


@pytest.mark.parametrize('side',['LONG','SHORT'])
@pytest.mark.parametrize('failure',['close','protection'])
def test_context_warning_never_overrides_price_failure_or_revives_closed_plan(monkeypatch,side,failure):
    state,_=armed(monkeypatch,side)
    state,_=run_plan(state,24*D,side,extra_events=[contrary(side)],reaction_override=reaction(side))
    kw=dict(close_failure=failure=='close',protection_failure=failure=='protection')
    state,ev=run_plan(state,25*D,side,extra_events=[contrary(side),contrary(side,25)],reaction_override=reaction(side),**kw)
    assert any(e['stage']=='PLANO_INVALIDADO' for e in ev)
    assert not any(e['stage']=='PLANO_EM_ATENCAO' for e in ev)
    closed=next(iter(state['captures'].values())).get('plan')
    assert closed is None or closed['invalidated']
    frozen=copy.deepcopy(state)
    state,ev=run_plan(state,25*D,side)
    assert state==frozen and not ev


def test_unarmed_primary_retains_original_reaction_cancellation(monkeypatch):
    state,_=armed(monkeypatch);p=next(iter(state['captures'].values()))['plan']
    for k in ('armed_ts','m15_armed_ts','m15_zone'):p.pop(k,None)
    state,ev=run_plan(state,24*D,reaction_override=reaction())
    assert any(e.get('reason')=='CONFIRMED_REACTION_CHANGED' for e in ev)
    assert not any(e['stage']=='PLANO_EM_ATENCAO' for e in ev)


def test_stored_invalidated_plan_is_not_reopened_by_mode2(monkeypatch):
    state,_=armed(monkeypatch);next(iter(state['captures'].values()))['plan']['invalidated']=True
    state,ev=run_plan(state,24*D)
    assert next(iter(state['captures'].values()))['plan']['invalidated']
    assert not any(e['stage'] in ('M15_ARMADO','RETESTE_OBSERVADO','PLANO_EM_ATENCAO') for e in ev)


def test_same_plan_context_warnings_coalesce_origins_without_losing_receipts(monkeypatch):
    from kairos_alert_presentation import bundle_events
    state,_=armed(monkeypatch)
    state,ev=run_plan(state,24*D,reaction_override=reaction())
    first=next(e for e in ev if e['stage']=='PLANO_EM_ATENCAO')
    other=copy.deepcopy(first);other['capture_id']='independent-origin'
    other['context_reasons'][0]['level']=100.0
    other['plan_details']['m5_zone']=dict(id='optional-child',tipo='FVG_bullish',bottom=100.3,top=100.4)
    other['plan_details']['had_retest']=not first['plan_details']['had_retest']
    groups=bundle_events([(1,first),(2,other)])
    assert len(groups)==1 and groups[0]['seqs']==[1,2]
    text='\n'.join(format_event_group(groups[0]['events'],now_ts=24*D))
    assert text.count('PLANO EM ATENÇÃO')==1
    assert '99,80' in text and '100,00' in text


def test_catchup_terminal_suppresses_obsolete_maintained_warning_and_acks_both(tmp_path,monkeypatch):
    import sqlite3,json
    state,_=armed(monkeypatch)
    monkeypatch.setenv('KAIROS_ENTRY_STRUCTURE','LUDO')
    state,ev=run_plan(state,25*D,extra_events=[contrary()],reaction_override=reaction(),close_failure=True)
    selected=[e for e in ev if e['stage'] in ('PLANO_EM_ATENCAO','PLANO_INVALIDADO')]
    assert [e['stage'] for e in selected]==['PLANO_EM_ATENCAO','PLANO_INVALIDADO']
    path=str(tmp_path/'warnings.db')
    db=chains._db(path)
    for e in selected:db.execute('INSERT INTO chain_outbox(key,pair,payload) VALUES(?,?,?)',(e['key'],'SOLUSD',json.dumps(e)))
    db.commit();db.close();sent=[]
    monkeypatch.setattr(chains.time,'time',lambda:25*D/1000)
    assert chains.deliver('SOLUSD',lambda page:sent.append(page) or True,path)
    text='\n'.join(sent)
    assert 'PLANO INVALIDADO' in text and 'Plano mantido' not in text
    with sqlite3.connect(path) as db:assert db.execute('SELECT ack FROM chain_outbox ORDER BY seq').fetchall()==[(1,),(1,)]


def test_reaction_warning_records_and_explains_new_context(monkeypatch):
    state,_=armed(monkeypatch)
    state,ev=run_plan(state,24*D,reaction_override=reaction())
    notice=next(e for e in ev if e['stage']=='PLANO_EM_ATENCAO')
    reason=notice['context_reasons'][0]
    assert reason['reaction_state']=='ACCEPTANCE_CONTINUATION' and reason['reaction_direction']=='SHORT'
    text='\n'.join(format_event_group([notice],now_ts=24*D))
    assert 'aceitação / continuação' in text and 'VENDA (contexto)' in text
