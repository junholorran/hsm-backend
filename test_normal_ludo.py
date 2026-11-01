import copy
from types import SimpleNamespace
import pytest
from test_m15_primary import inputs,run_native,M15
import kairos_m15_primary as primary

@pytest.mark.parametrize('side',['UP','DOWN'])
def test_normal_direct_fvg_does_not_require_force(side):
    rows,c,e,z=inputs();rows[21].update(o=101.04,h=101.5,l=101,c=101.1)
    if side=='DOWN':
        def mirror(c):return dict(c,o=200-c['o'],h=200-c['l'],l=200-c['h'],c=200-c['c'])
        rows=[mirror(c) for c in rows]
        e=dict(e,side='DOWN',level=200-e['level'],close=200-e['close'],opposite_level=200-e['opposite_level'])
        z=dict(z,tipo='FVG_bearish',direcao='baixa',bottom=200-z['top'],top=200-z['bottom'],source_a=rows[-3],source_mid=rows[-2],source_c=rows[-1])
    en=SimpleNamespace(_kairos_fvg_states=lambda *a,**k:[z])
    assert primary.strong_break(rows,e) is False
    assert primary.direct_zone(rows,e,23*M15,en)==z


def test_normal_break_arms_causal_plan_and_marks_no_fire(monkeypatch):
    monkeypatch.setenv('KAIROS_M15_PRIMARY_FVG','true')
    state,_=run_native({},20*M15,weak=True)
    for cut in (21*M15,22*M15):state,events=run_native(state,cut,weak=True)
    assert any(e['stage']=='M15_QUEBRA' for e in events)
    assert events[-1]['break_strong'] is False
    state,events=run_native(state,23*M15,weak=True)
    ready=next(e for e in events if e['stage']=='M15_ARMADO')
    assert ready['break_strong'] is False
    from kairos_notice_context import format_entry_notice
    text='\n'.join(format_entry_notice([ready],now_ts=23*M15))
    assert 'PREPARAR ORDEM LIMITE' in text and '🔥' not in text
    assert 'COMPRA' in text

@pytest.mark.parametrize('strong',[False,True])
def test_fire_depends_on_classification_not_primary_mode(strong):
    from test_compact_entry_notice import proof,render
    from kairos_notice_brief import format_event_group
    e=proof(stage='M15_ARMADO');e.update(primary_policy='M15_PRIMARY_LUDO_FVG_V2',break_strong=strong)
    text='\n'.join(render([e]));assert ('🔥' in text)==strong
    e['stage']='M15_QUEBRA'
    assert ('🔥' in '\n'.join(format_event_group([e],now_ts=e['observed_ts'])))==strong


def test_legacy_force_events_keep_badge_but_unknown_new_events_do_not():
    assert primary.notice_is_strong({'primary_policy':primary.LEGACY_STRONG_POLICY})
    assert not primary.notice_is_strong({'primary_policy':primary.POLICY})
    assert not primary.notice_is_strong({'primary_policy':primary.LEGACY_STRONG_POLICY,'break_strong':False})

@pytest.mark.parametrize('side',['UP','DOWN'])
def test_normal_break_rejects_already_breached_protection(side):
    rows,c,e,z=inputs()
    if side=='UP':rows[21]['l']=99.2
    else:
        rows=[dict(c,o=200-c['o'],h=200-c['l'],l=200-c['h'],c=200-c['c']) for c in rows]
        e=dict(e,side='DOWN',level=200-e['level'],opposite_level=200-e['opposite_level'])
        rows[21]['h']=100.8
    assert not primary.valid_break(rows,e)


def test_no_backfill_or_repeat_after_switch_to_normal(monkeypatch):
    monkeypatch.setenv('KAIROS_M15_PRIMARY_FVG','true')
    state,_=run_native({},20*M15)
    for cut in (21*M15,22*M15,23*M15):state,_=run_native(state,cut)
    before=copy.deepcopy(state)
    after,events=run_native(state,23*M15,weak=True)
    assert after==before and events==[]


def test_reaction_still_must_precede_normal_break(monkeypatch):
    monkeypatch.setenv('KAIROS_M15_PRIMARY_FVG','true')
    late=dict(key='late',state='REJECTION_RECLAIM',direction='LONG',confirmed_ts=23*M15,leg_start_ts=20*M15)
    state,_=run_native({},20*M15,weak=True,rx_override=late)
    events=[]
    for cut in (21*M15,22*M15,23*M15):
        state,ev=run_native(state,cut,weak=True,rx_override=late);events+=ev
    assert not any(e['stage'] in ('M15_QUEBRA','M15_ARMADO') for e in events)


def test_normal_short_transition_and_notice(monkeypatch):
    import test_m15_primary as fixture
    rows,c,e,z=inputs();rows[21].update(o=101.04,h=101.5,l=101,c=101.1)
    def mirror(bar):return dict(bar,o=200-bar['o'],h=200-bar['l'],l=200-bar['h'],c=200-bar['c'])
    rows=[mirror(bar) for bar in rows]
    c=dict(c,liquidity_side='HIGH',liquidity_type='PDH',nivel=200-c['nivel'])
    e=dict(e,side='DOWN',level=200-e['level'],close=200-e['close'],opposite_level=200-e['opposite_level'])
    z=dict(z,tipo='FVG_bearish',direcao='baixa',bottom=200-z['top'],top=200-z['bottom'],source_a=rows[-3],source_mid=rows[-2],source_c=rows[-1])
    monkeypatch.setenv('KAIROS_M15_PRIMARY_FVG','true')
    monkeypatch.setattr(fixture,'inputs',lambda:copy.deepcopy((rows,c,e,z)))
    original_bars=fixture.bars
    monkeypatch.setattr(fixture,'bars',lambda *a,**k:[mirror(bar) for bar in original_bars(*a,**k)])
    rx=dict(key='short',state='REJECTION_RECLAIM',direction='SHORT',confirmed_ts=21*M15,leg_start_ts=20*M15)
    state,_=run_native({},20*M15,rx_override=rx)
    stages=[]
    for cut in (21*M15,22*M15,23*M15):
        state,events=run_native(state,cut,rx_override=rx);stages.extend(e['stage'] for e in events)
    assert 'M15_QUEBRA' in stages and 'M15_ARMADO' in stages
    ready=next(e for e in events if e['stage']=='M15_ARMADO')
    assert ready['direction']=='SHORT' and ready['break_strong'] is False
    from kairos_notice_context import format_entry_notice
    text='\n'.join(format_entry_notice([ready],now_ts=23*M15))
    assert 'VENDA' in text and '🔥' not in text


def test_persisted_strict_plan_still_arms_and_retests_with_original_badge(monkeypatch):
    monkeypatch.setenv('KAIROS_M15_PRIMARY_FVG','true')
    state,_=run_native({},20*M15)
    for cut in (21*M15,22*M15):state,_=run_native(state,cut)
    plan=next(iter(state['captures'].values()))['plan']
    plan['primary_policy']=primary.LEGACY_STRONG_POLICY;plan.pop('break_strong')
    state,events=run_native(state,23*M15)
    ready=next(e for e in events if e['stage']=='M15_ARMADO')
    assert ready['primary_policy']==primary.LEGACY_STRONG_POLICY and ready['break_strong'] is True
    state,events=run_native(state,23*M15+300000)
    assert [e['stage'] for e in events]==['RETESTE_OBSERVADO']
    assert events[0]['break_strong'] is True


def test_detailed_touch_notice_does_not_require_force():
    from test_organized_alerts import proof
    from kairos_alert_presentation import format_chain_events
    e=proof(stage='POI_HTF_TOCADO')
    text='\n'.join(format_chain_events([e],now_ts=e['observed_ts']))
    assert 'com força' not in text
