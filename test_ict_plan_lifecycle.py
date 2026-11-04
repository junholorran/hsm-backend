from types import SimpleNamespace
import copy
import pytest
import kairos_capture_chains as chains
from kairos_ludo_fvg import fvg_states

D=900000

def sample(side='LONG',close=101.4,wick=100.5):
    rows=[dict(t=0,o=100,h=101,l=99,c=100.5),
          dict(t=D,o=100.5,h=104,l=100,c=103.5),
          dict(t=2*D,o=103.5,h=105,l=103,c=104)]
    z=next(z for z in fvg_states(rows) if z['tipo']=='FVG_bullish')
    rows.append(dict(t=3*D,o=102,h=103,l=wick,c=close))
    if side=='SHORT':
        def flip(c):return dict(c,o=200-c['o'],h=200-c['l'],l=200-c['h'],c=200-c['c'])
        rows=[flip(c) for c in rows]
        z=next(z for z in fvg_states(rows[:3]) if z['tipo']=='FVG_bearish')
    return rows,z

EN=SimpleNamespace(_kairos_fvg_states=fvg_states)

@pytest.mark.parametrize('side',['LONG','SHORT'])
def test_wick_removes_ludo_box_but_not_armed_plan(side):
    rows,z=sample(side)
    assert next(x for x in fvg_states(rows) if x['id']==z['id'])['state']=='INVALIDADA'
    assert chains._plan_removal(z,rows,D,EN) is None

@pytest.mark.parametrize('side',['LONG','SHORT'])
def test_strict_close_through_returns_actual_failure_evidence(side):
    rows,z=sample(side,close=100.8)
    r=chains._plan_removal(z,rows,D,EN)
    assert r['confirmed_ts']==4*D and r['basis']=='FVG_CLOSE_THROUGH'
    assert r['level']==(101 if side=='LONG' else 99)
    assert r['close']==rows[-1]['c']
    assert r['bottom']==z['bottom'] and r['top']==z['top']

@pytest.mark.parametrize('side',['LONG','SHORT'])
def test_close_on_boundary_does_not_mean_beyond(side):
    rows,z=sample(side,close=101)
    assert chains._plan_removal(z,rows,D,EN) is None


def test_history_eviction_is_not_price_failure():
    rows,z=sample()
    assert chains._plan_removal(z,rows,D,SimpleNamespace(_kairos_fvg_states=lambda *a,**k:[])) is None


def test_missing_source_evidence_fails_closed_explicitly():
    rows,z=sample()
    r=chains._plan_removal(z,rows[1:],D,EN)
    assert r['basis']=='FVG_EVIDENCE_MISSING'


def test_non_ludo_native_rules_stay_unchanged():
    rows,z=sample();z=dict(z,ludo_fvg=False)
    assert chains._plan_removal(z,rows,D,EN)['confirmed_ts']==4*D

@pytest.mark.parametrize('damage',['changed_source','gap','bad_geometry','nonfinite'])
def test_unverifiable_saved_ludo_zone_cannot_keep_plan_alive(damage):
    rows,z=sample();z=copy.deepcopy(z)
    if damage=='changed_source':z['source_a']['c']+=0.01
    elif damage=='gap':rows.append(dict(rows[-1],t=5*D))
    elif damage=='bad_geometry':z['bottom']+=0.01
    else:rows[-1]['c']=float('nan')
    assert chains._plan_removal(z,rows,D,EN)['basis']=='FVG_EVIDENCE_MISSING'


def run_plan(state,cutoff,side='LONG',close_failure=False,protection_failure=False):
    from unittest.mock import patch
    from test_m15_primary import inputs
    import kairos_m15_primary as primary
    rows,c,e,_=inputs()
    z=next(x for x in fvg_states(rows) if x['created_ts']==22*D)
    rows += [dict(t=23*D,o=100.9,h=101,l=100.1,c=100.4),
             dict(t=24*D,o=100.4,h=100.7,l=99.3 if protection_failure else 100,
                  c=100.1 if close_failure else 100.4)]
    if side=='SHORT':
        rows=[dict(r,o=200-r['o'],h=200-r['l'],l=200-r['h'],c=200-r['c']) for r in rows]
        c=dict(c,liquidity_side='HIGH',liquidity_type='PDH',nivel=200-c['nivel'])
        e=dict(e,side='DOWN',level=200-e['level'],close=200-e['close'],opposite_level=200-e['opposite_level'])
        z=next(x for x in fvg_states(rows[:23]) if x['created_ts']==22*D)
    m15=[r for r in rows if r['t']+D<=cutoff]
    m5=[dict(r,t=r['t']+j*chains.M5) for r in rows for j in range(3) if r['t']+(j+1)*chains.M5<=cutoff]
    rx=dict(key='rx',state='REJECTION_RECLAIM',direction=side,confirmed_ts=21*D,leg_start_ts=20*D)
    engine=SimpleNamespace(_kairos_fvg_states=fvg_states,_kairos_m5_refine_zone=lambda *a,**k:None,
                           _kairos_select_entry_zone=lambda *a,**k:None)
    def snapshot(cs,d,cut):return dict(status='OK',events=[e] if d==D and cut>=e['close_ts'] else [])
    with patch.object(chains,'structure_snapshot',side_effect=snapshot), \
         patch.object(chains,'reaction_timeline',return_value=[]), \
         patch.object(chains,'evidence_at',side_effect=lambda tl,ts:rx if ts>=21*D else None):
        return chains.advance(state,'SOLUSD',[c],dict(M15=m15,M5=m5),cutoff,engine,observed_ts=cutoff)

@pytest.mark.parametrize('side',['LONG','SHORT'])
def test_armed_plan_survives_wick_then_closes_with_evidence_and_same_identity(monkeypatch,side):
    from kairos_notice_brief import plan_ref,format_event_group
    monkeypatch.setenv('KAIROS_M15_PRIMARY_FVG','true')
    state={}
    for n in range(20,24):state,ev=run_plan(state,n*D,side)
    armed=next(x for x in ev if x['stage']=='M15_ARMADO')
    ref=plan_ref(armed)
    state,ev=run_plan(state,24*D,side)
    assert any(x['stage']=='RETESTE_OBSERVADO' for x in ev)
    assert not next(iter(state['captures'].values()))['plan']['invalidated']
    state,ev=run_plan(state,25*D,side,close_failure=True)
    invalid=next(x for x in ev if x['stage']=='PLANO_INVALIDADO')
    assert plan_ref(invalid)==ref and invalid['fill_assumed'] is False
    reason=next(r for r in invalid['invalidation_reasons'] if r.get('basis')=='FVG_CLOSE_THROUGH')
    assert reason['confirmed_ts']==25*D
    notice='\n'.join(format_event_group([invalid],now_ts=25*D))
    assert 'Zona M15:' in notice and 'Quebra original:' in notice
    assert 'Fechamento M15' in notice and 'borda' in notice
    frozen=copy.deepcopy(state)
    state,ev=run_plan(state,25*D,side,close_failure=True)
    assert state==frozen and ev==[]


def test_protected_extreme_still_cancels_even_when_native_close_recovers(monkeypatch):
    monkeypatch.setenv('KAIROS_M15_PRIMARY_FVG','true')
    state={}
    for n in range(20,25):state,ev=run_plan(state,n*D)
    state,ev=run_plan(state,25*D,protection_failure=True)
    assert any(r['code']=='STRUCTURAL_EXTREME_BREACHED' for x in ev for r in x.get('invalidation_reasons',[]))
    assert next(iter(state['captures'].values()))['plan']['invalidated']


def armed_state(monkeypatch):
    monkeypatch.setenv('KAIROS_M15_PRIMARY_FVG','true')
    state={}
    for n in range(20,24):state,ev=run_plan(state,n*D)
    return state


def test_unarmed_legacy_parent_keeps_native_wick_invalidation(monkeypatch):
    state=armed_state(monkeypatch)
    p=next(iter(state['captures'].values()))['plan']
    for k in ('primary_policy','m15_zone','m15_armed_ts','armed_ts'):p.pop(k,None)
    monkeypatch.setenv('KAIROS_M15_PRIMARY_FVG','false')
    state,ev=run_plan(state,24*D)
    assert next(iter(state['captures'].values()))['plan']['invalidated']
    assert not any(x['stage'] in ('M5_ARMADO','RETESTE_OBSERVADO') for x in ev)


def test_missing_abc_cannot_emit_positive_retest_during_catchup(monkeypatch):
    state=armed_state(monkeypatch)
    p=next(iter(state['captures'].values()))['plan']
    p['parent_zone']['source_a']['c']+=0.01
    state,ev=run_plan(state,25*D)
    assert not any(x['stage']=='RETESTE_OBSERVADO' for x in ev)
    assert any(r.get('basis')=='FVG_EVIDENCE_MISSING' for x in ev for r in x.get('invalidation_reasons',[]))


def test_proven_price_failure_is_not_masked_by_later_bad_data():
    rows,z=sample(close=100.8)
    rows.append(dict(t=4*D,o=101,h=102,l=100,c=float('nan')))
    r=chains._plan_removal(z,rows,D,EN)
    assert r['basis']=='FVG_CLOSE_THROUGH' and r['confirmed_ts']==4*D


@pytest.mark.parametrize('damage',['missing','different'])
def test_bad_touch_zone_fails_closed_instead_of_throwing_or_touching(monkeypatch,damage):
    state=armed_state(monkeypatch)
    p=next(iter(state['captures'].values()))['plan']
    if damage=='missing':p['m15_zone'].pop('bottom')
    else:p['m15_zone']['bottom']+=0.1
    state,ev=run_plan(state,24*D)
    assert not any(x['stage']=='RETESTE_OBSERVADO' for x in ev)
    assert next(iter(state['captures'].values()))['plan']['invalidated']
    assert any(r.get('basis')=='FVG_EVIDENCE_MISSING' for x in ev for r in x.get('invalidation_reasons',[]))
    from kairos_notice_brief import format_event_group
    for x in ev:format_event_group([x],now_ts=24*D)


def test_m5_failure_notice_identifies_actual_m5_range_with_parent_present():
    from kairos_notice_brief import format_event_group
    e=dict(pair='ETHUSD',stage='PLANO_INVALIDADO',direction='SHORT',break_ts=22*D,observed_ts=25*D,
           parent_zone=dict(bottom=110,top=120),zone=dict(bottom=112,top=114),
           invalidation_reasons=[dict(code='M5_POI_INVALIDATED',zone_tf='M5',basis='FVG_CLOSE_THROUGH',
                                     level=114,close=115,confirmed_ts=25*D)])
    text='\n'.join(format_event_group([e],now_ts=25*D))
    assert 'Zona M5: 112,00–114,00' in text
