import copy
from unittest.mock import patch
import pytest
import kairos_capture_chains as chains
import kairos_m15_primary as primary
from kairos_fvg_force_shadow import _metrics
from test_capture_chains import M15,M5,bars,capture,event,Engine


def inputs():
    # 20 baseline candles, sell-side capture then strong bullish break B.
    rows=[dict(t=i*M15,o=100,h=100.2,l=99.9,c=100.1) for i in range(20)]
    rows += [dict(t=20*M15,o=100,h=100.2,l=99.4,c=100.1),
             dict(t=21*M15,o=100.1,h=101.2,l=100,c=101.1),
             dict(t=22*M15,o=101.1,h=101.3,l=100.8,c=100.9)]
    c=dict(liquidity_tf='D1',liquidity_type='PDL',liquidity_side='LOW',nivel=99.8,
           liquidity_origin_ts=0,first_capture_ts=20*M15,native_capture_confirm_ts=21*M15)
    e=dict(kind='CHoCH',side='UP',level=100.5,origin_ts=19*M15,
           level_known_ts=20*M15,open_ts=21*M15,close_ts=22*M15,close=101.1,
           opposite_level=99.4,opposite_origin_ts=20*M15)
    z=dict(id='native',tipo='FVG_bullish',direcao='alta',bottom=100.2,top=100.8,
           created_ts=22*M15,lux_fvg=True,state='ATIVA',source_a=rows[-3],source_mid=rows[-2],source_c=rows[-1])
    return rows,c,e,z


def test_strong_break_waits_for_c_then_arms_m15_without_m5(monkeypatch):
    monkeypatch.setenv('KAIROS_M15_PRIMARY_FVG','true')
    rows,c,e,z=inputs()
    assert primary.strong_break(rows,e) is True
    assert primary.direct_zone(rows[:-1],e,22*M15,Engine) is None
    class Native:
        _kairos_fvg_states=staticmethod(lambda *a,**k:[z])
    assert primary.direct_zone(rows,e,23*M15,Native)['id']=='native'
    plan=dict(primary_policy=primary.POLICY,break_=e)


def test_weak_break_cannot_qualify_despite_earlier_strong_gap():
    rows,c,e,z=inputs();rows[-2].update(o=101.04,h=101.5,l=101,c=101.1)
    assert primary.strong_break(rows,e) is False


def test_primary_view_does_not_require_m5_and_queued_legacy_is_suppressed(monkeypatch):
    monkeypatch.setenv('KAIROS_M15_PRIMARY_FVG','true')
    rows,c,e,z=inputs()
    plan=dict(primary_policy=primary.POLICY,break_=e)
    plan.update({'break':e,'parent_zone':z,'m15_zone':z,'m15_armed_ts':23*M15,'armed_ts':23*M15,
                 'invalidation':99.4,'break_ts':22*M15,'invalidated':False})
    state=dict(causal_policy_version=chains.CAUSAL_POLICY_VERSION,asof_ts=23*M15,
               captures={'x':dict(capture=c,plan=plan)})
    view=chains.state_view(state)
    assert view['setup']['tf']=='M15'
    assert view['setup']['level']==100.8
    assert not chains._entry_notice_allowed(dict(stage='M5_ARMADO',capture=c,direction='LONG',zone=z))
    assert chains._entry_notice_allowed(dict(stage='M15_ARMADO',capture=c,direction='LONG',zone=z,
                                             primary_policy=primary.POLICY))

def run_native(state,cutoff,weak=False,with_gap=True,extra_events=(),rx_override=None):
    rows,c,e,z=inputs()
    if weak:rows[21].update(o=101.04,h=101.5,l=101,c=101.1)
    rows += [dict(t=t,o=101,h=101.5,l=100.9,c=101.2) for t in range(23*M15,cutoff,M15)]
    tf=dict(M15=[r for r in rows if r['t']+M15<=cutoff],M5=bars(cutoff,M5,100.9))
    class Native:
        _kairos_fvg_states=staticmethod(lambda cs,**kw:[z] if with_gap and len(cs)>1 and cs[1]['t']-cs[0]['t']==M15 else [])
        _kairos_m5_refine_zone=staticmethod(lambda *a,**kw:None)
        _kairos_select_entry_zone=staticmethod(lambda *a,**kw:None)
    rx=dict(key='rx',state='REJECTION_RECLAIM',direction='LONG',confirmed_ts=21*M15,leg_start_ts=20*M15)
    if rx_override:rx=rx_override
    def snapshot(cs,d,cut):return dict(status='OK',events=[q for q in [e]+list(extra_events) if q['close_ts']<=cut] if d==M15 else [])
    with patch.object(chains,'structure_snapshot',side_effect=snapshot), \
         patch.object(chains,'reaction_timeline',return_value=[]), \
         patch.object(chains,'evidence_at',side_effect=lambda tl,ts:rx if ts>=21*M15 else None):
        return chains.advance(state,'SOLUSD',[c],tf,cutoff,Native,observed_ts=cutoff)


def test_full_transition_arms_before_retest_without_refinement_and_deduplicates(monkeypatch):
    monkeypatch.setenv('KAIROS_M15_PRIMARY_FVG','true')
    state,_=run_native({},20*M15)
    state,_=run_native(state,21*M15)
    state,events=run_native(state,22*M15)
    assert [e['stage'] for e in events]==['M15_QUEBRA']
    state,events=run_native(state,23*M15)
    assert [e['stage'] for e in events]==['M15_ARMADO']
    plan=next(iter(state['captures'].values()))['plan']
    assert plan['m15_zone'] and not plan.get('m5_zone') and not plan.get('touched')
    assert chains.state_view(state)['setup']['tf']=='M15'
    frozen=copy.deepcopy(state)
    state,events=run_native(state,23*M15)
    assert state==frozen and events==[]
    state,events=run_native(state,23*M15+M5)
    assert [e['stage'] for e in events]==['RETESTE_OBSERVADO']
    assert events[0]['zone_tf']=='M15' and events[0]['fill_assumed'] is False


def test_full_transition_weak_break_or_absent_gap_does_not_arm(monkeypatch):
    monkeypatch.setenv('KAIROS_M15_PRIMARY_FVG','true')
    for weak,gap in [(True,True),(False,False)]:
        state,_=run_native({},20*M15,weak,gap)
        events=[]
        for cut in (21*M15,22*M15,23*M15):
            state,ev=run_native(state,cut,weak,gap);events+=ev
        assert not any(e['stage'] in ('M15_ARMADO','M5_ARMADO','RETESTE_OBSERVADO') for e in events)
        if not weak:assert any(e.get('reason')=='NO_DIRECT_M15_FVG' for e in events)


def test_primary_presentation_labels_m15_and_m5_optional(monkeypatch):
    monkeypatch.setenv('KAIROS_M15_PRIMARY_FVG','true')
    state,_=run_native({},20*M15)
    for cut in (21*M15,22*M15,23*M15):state,events=run_native(state,cut)
    from kairos_alert_presentation import format_chain_events
    text='\n'.join(format_chain_events(events,now_ts=23*M15))
    assert 'PLANO M15' in text and 'M5 opcional' in text
    assert 'M5 FVG comprador' not in text

def test_legacy_plan_retires_once_and_new_retest_is_not_claimed(monkeypatch):
    monkeypatch.setenv('KAIROS_M15_PRIMARY_FVG','true')
    state,_=run_native({},20*M15)
    state,_=run_native(state,21*M15)
    rows,c,e,z=inputs()
    rec=next(iter(state['captures'].values()))
    rec['plan']={'break':e,'break_ts':22*M15,'parent_zone':z,'invalidation':99.4,
                 'invalidated':False,'armed_ts':21*M15}
    state,events=run_native(state,22*M15)
    assert sum(e.get('reason')=='ENTRY_POLICY_CHANGED' for e in events)==1
    state,events=run_native(state,22*M15)
    assert events==[]


def test_primary_plan_ignores_invalid_optional_m5_zone(monkeypatch):
    monkeypatch.setenv('KAIROS_M15_PRIMARY_FVG','true')
    state,_=run_native({},20*M15)
    for cut in (21*M15,22*M15,23*M15):state,_=run_native(state,cut)
    rec=next(iter(state['captures'].values()))
    rec['plan']['m5_zone']={'id':'dead-child','tipo':'FVG_bullish','lux_fvg':True,
                           'bottom':100.3,'top':100.4,'created_ts':21*M15}
    state,events=run_native(state,23*M15+M5)
    assert not any(e['stage']=='PLANO_INVALIDADO' for e in events)
    assert not next(iter(state['captures'].values()))['plan']['invalidated']

def test_outside_break_cannot_advertise_already_breached_protection():
    rows,c,e,z=inputs();rows[21]['l']=99.2
    assert primary.strong_break(rows,e) is False
    mirrored=[dict(r,o=200-r['o'],h=200-r['l'],l=200-r['h'],c=200-r['c']) for r in rows]
    short=dict(e,side='DOWN',level=99.5,opposite_level=100.6)
    assert primary.strong_break(mirrored,short) is False


def test_mode_switch_has_prospective_baseline_and_reversible_retirement(monkeypatch):
    monkeypatch.setenv('KAIROS_M15_PRIMARY_FVG','true')
    state,_=run_native({},20*M15)
    assert state['m15_primary_since']==20*M15
    for cut in (21*M15,22*M15,23*M15):state,_=run_native(state,cut)
    monkeypatch.delenv('KAIROS_M15_PRIMARY_FVG')
    state,events=run_native(state,23*M15+M5)
    assert any(e.get('reason')=='ENTRY_POLICY_CHANGED' for e in events)
    assert not chains._entry_notice_allowed(dict(stage='M15_ARMADO',capture=inputs()[1],direction='LONG',zone=inputs()[3],primary_policy=primary.POLICY))

def test_opposite_m15_cancels_primary_and_reaction_touch_is_ambiguous(monkeypatch):
    monkeypatch.setenv('KAIROS_M15_PRIMARY_FVG','true')
    state,_=run_native({},20*M15)
    for cut in (21*M15,22*M15,23*M15):state,_=run_native(state,cut)
    ready=copy.deepcopy(state)
    opposite=dict(inputs()[2],side='DOWN',open_ts=23*M15,close_ts=24*M15,
                  level_known_ts=22*M15,opposite_level=101.3)
    state,events=run_native(state,24*M15,extra_events=[opposite])
    assert any(e.get('reason')=='OPPOSITE_M15_BREAK' for e in events)
    newrx=dict(key='newrx',state='ACCEPTANCE_CONTINUATION',direction='SHORT',
               confirmed_ts=23*M15+M5,leg_start_ts=23*M15)
    state,events=run_native(ready,23*M15+M5,rx_override=newrx)
    assert any(e['stage']=='TOQUE_E_INVALIDACAO' and e.get('reason')=='CONFIRMED_REACTION_CHANGED' for e in events)
    assert not any(e['stage']=='RETESTE_OBSERVADO' for e in events)

def test_optional_refinement_error_cannot_block_primary_arm(monkeypatch):
    monkeypatch.setenv('KAIROS_M15_PRIMARY_FVG','true')
    state,_=run_native({},20*M15)
    for cut in (21*M15,22*M15):state,_=run_native(state,cut)
    with patch.object(chains,'_refinement',side_effect=RuntimeError('optional unavailable')):
        state,events=run_native(state,23*M15)
    assert any(e['stage']=='M15_ARMADO' for e in events)
    assert chains.state_view(state)['setup']['tf']=='M15'

def test_same_cutoff_legacy_view_cannot_advertise_old_actionable_plan(monkeypatch):
    monkeypatch.setenv('KAIROS_M15_PRIMARY_FVG','true')
    rows,c,e,z=inputs()
    plan={'break':e,'break_ts':e['close_ts'],'parent_zone':z,'m5_zone':z,
          'armed_ts':23*M15,'invalidation':99.4,'invalidated':False}
    state=dict(causal_policy_version=chains.CAUSAL_POLICY_VERSION,asof_ts=23*M15,
               captures={'old':dict(capture=c,plan=plan)})
    view=chains.state_view(state)
    assert view['setup'] is None
    assert view['code']=='SETUP_ANTERIOR_INVALIDADO'
