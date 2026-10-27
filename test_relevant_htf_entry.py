import copy,json
from unittest.mock import patch
import pytest
import kairos_capture_chains as chains
from test_capture_chains import capture,event,bars,Engine,M5,M15,advance
from test_ludo_causal_episodes import initial,run

@pytest.mark.parametrize('tf,kind',[('M15','LUDO_SWING_HIGH'),('M5','SWING_HIGH'),('D1','SWING_HIGH'),('W1','EQH')])
def test_local_or_unspecified_macro_pool_cannot_authorize_entry(tf,kind):
    c={**capture(),'liquidity_tf':tf,'liquidity_type':kind}
    s,_=run({},M15,captures=[c]);s,_=run(s,2*M15,captures=[c])
    _,es=run(s,3*M15,captures=[c],events=[event()])
    assert not any(e['stage'] in ('M15_QUEBRA','M5_ARMADO') for e in es)


def test_standalone_htf_fvg_touch_is_not_a_liquidity_entry():
    c=dict(origin_kind='HTF_FVG_TOUCH',origin_key='H1|fvg',htf_location={'tf':'H1'},
           origin_zone={'tipo':'FVG_bearish','direcao':'baixa'},first_capture_ts=M15,native_capture_confirm_ts=2*M15)
    s,_=run({},M15,captures=[c]);s,_=run(s,2*M15,captures=[c])
    _,es=run(s,3*M15,captures=[c],events=[event()])
    assert not any(e['stage'] in ('M15_QUEBRA','M5_ARMADO') for e in es)


def test_ob_or_ifvg_refinement_cannot_be_an_entry_plan():
    for kind in ('OB_bearish','IFVG_bearish'):
        class NoFvg(Engine):
            @staticmethod
            def _kairos_fvg_states(cs,lookback=250):
                return [dict(id='parent',state='ATIVA'),dict(id='child',state='ATIVA',tipo=kind)]
            @staticmethod
            def _kairos_m5_refine_zone(*args,**kw):
                return {**Engine._kairos_m5_refine_zone(*args,**kw),'tipo':kind}
        tf={'M15':bars(3*M15,M15),'M5':bars(3*M15,M5)}
        with patch.object(chains,'structure_snapshot',return_value=dict(status='OK',events=[event()])):
            _,es=chains.advance(initial(),'SOLUSD',[capture()],tf,3*M15,NoFvg)
        assert not any(e['stage']=='M5_ARMADO' for e in es)


def test_m15_origin_existing_plan_and_queue_cannot_survive_policy():
    s,es=run(initial(),3*M15,events=[event()])
    rec=next(iter(s['captures'].values()));rec['capture']['liquidity_tf']='M15'
    assert chains.state_view(s)['setup'] is None
    _,es=run(s,4*M15,events=[event()])
    assert any(e.get('reason')=='ENTRY_POLICY_CHANGED' for e in es)


@pytest.mark.parametrize('tf,kind',[('D1','PDH'),('W1','PWH'),('MN','PMH'),('H4','SWING_HIGH'),('H1','EQH')])
def test_relevant_high_sweep_can_still_arm_causal_fvg(tf,kind):
    c={**capture(),'liquidity_tf':tf,'liquidity_type':kind}
    s,_=run({},M15,captures=[c]);s,_=run(s,2*M15,captures=[c])
    _,es=run(s,3*M15,captures=[c],events=[event()])
    assert any(e['stage']=='M5_ARMADO' and e['direction']=='SHORT' for e in es)


def test_monthly_origin_uses_exact_existing_m15_capture_only():
    from kairos_reaction_episode import period_capture_origins
    class Calendar:
        _kairos_previous_period_liquidity=staticmethod(lambda *a:[dict(tf='MN',type='PMH',level=108,origin_ts=-100,confirmed_ts=0,captured_ts=M15,capture_confirm_ts=2*M15,captured_tf='M15')])
    rows=bars(2*M15,M15);rows[-1]['h']=111
    cap=period_capture_origins(Calendar,{'M15':rows},2*M15)[0]
    assert cap['native_capture_confirm_ts']==2*M15
    assert cap['first_capture_ts']==M15 and cap['liquidity_type']=='PMH'
    assert not period_capture_origins(Calendar,{'M15':bars(2*M15,M15)},2*M15-1)
    Calendar._kairos_previous_period_liquidity=staticmethod(lambda *a:[dict(tf='MN',type='PMH',level=108,origin_ts=-100,confirmed_ts=0,captured_ts=0,capture_confirm_ts=86400000,captured_tf='D1')])
    assert period_capture_origins(Calendar,{'M15':bars(2*M15,M15)},86400000)==[]


def test_newer_ifvg_cannot_hide_a_valid_causal_fvg():
    class Mixed(Engine):
        @staticmethod
        def _kairos_m5_refine_zone(*a,**kw):
            return {**Engine._kairos_m5_refine_zone(*a,**kw),'tipo':'IFVG_bearish'}
        @staticmethod
        def _kairos_fvg_states(cs,lookback=250):
            z=Engine._kairos_m5_refine_zone(None,None,None,None,'baixa')
            return [dict(id='parent',state='ATIVA'),{**z,'created_ts':M15+2*M5,'state':'ATIVA'}]
    with patch.object(chains,'structure_snapshot',return_value=dict(status='OK',events=[event()])):
        _,es=chains.advance(initial(),'SOLUSD',[capture()],{'M15':bars(3*M15,M15),'M5':bars(3*M15,M5)},3*M15,Mixed)
    armed=next(e for e in es if e['stage']=='M5_ARMADO')
    assert armed['zone']['tipo']=='FVG_bearish'


def test_period_adapter_rejects_missing_first_capture_history():
    from kairos_reaction_episode import period_capture_origins
    class Calendar:
        _kairos_previous_period_liquidity=staticmethod(lambda *a:[dict(tf='W1',type='PWH',level=108,origin_ts=-100,confirmed_ts=0,captured_ts=2*M15,capture_confirm_ts=3*M15,captured_tf='M15')])
    rows=bars(3*M15,M15)[1:];rows[-1].update(h=111,c=100)
    assert period_capture_origins(Calendar,{'M15':rows},3*M15)==[]


def test_invalid_latest_fvg_does_not_hide_older_valid_fvg():
    class Mixed(Engine):
        @staticmethod
        def _kairos_fvg_states(cs,lookback=250):
            base=Engine._kairos_m5_refine_zone(None,None,None,None,'baixa')
            return [{**base,'state':'INVALIDADA','invalidated_ts':3*M15},
                    {**base,'id':'older','state':'ATIVA','created_ts':M15+2*M5}]
    zone,_=chains._refinement(Mixed,bars(4*M15,M5),capture(),event(),dict(top=108,bottom=103),[])
    assert zone and zone['id']=='older'


def test_real_monthly_calendar_detector_feeds_only_precise_fresh_origin():
    import scalp_engine as engine
    from kairos_reaction_episode import period_capture_origins
    from test_monthly_capture_radar import MonthlyCaptureRadarTests
    start,tf=MonthlyCaptureRadarTests().fixtures()
    caps=period_capture_origins(engine,tf,start+2*M15)
    month=next(c for c in caps if c['liquidity_tf']=='MN')
    assert month['liquidity_type']=='PMH' and month['nivel']==120
    assert month['first_capture_ts']==start
    assert month['native_capture_confirm_ts']==start+M15


def test_native_daily_absence_can_prove_older_monthly_coverage():
    from kairos_reaction_episode import period_capture_origins
    day=86400000
    class Calendar:
        _kairos_previous_period_liquidity=staticmethod(lambda *a:[dict(tf='MN',type='PMH',level=108,origin_ts=-day,confirmed_ts=0,captured_ts=day,capture_confirm_ts=day+M15,captured_tf='M15')])
    daily=[dict(t=0,o=100,h=101,l=99,c=100)]
    rows=[dict(t=day,o=100,h=111,l=99,c=100)]
    assert len(period_capture_origins(Calendar,{'D1':daily,'M15':rows},day+M15))==1
    daily[0]['h']=109  # An older raid cannot become a fresh monthly capture.
    assert period_capture_origins(Calendar,{'D1':daily,'M15':rows},day+M15)==[]


def test_later_refinement_looks_past_invalid_latest_fvg():
    class Later(Engine):
        @staticmethod
        def _kairos_m5_refine_zone(*a,**kw):return None
        @staticmethod
        def _kairos_fvg_states(cs,lookback=250):
            base=dict(tipo='FVG_bearish',lux_fvg=True,direcao='baixa',top=106,bottom=104,created_ts=4*M15,
                      source_a={'t':4*M15-2*M5},source_mid={'t':4*M15-M5},source_c={'t':4*M15})
            return [{**base,'id':'bad','invalidated_ts':4*M15+M5}, {**base,'id':'valid','state':'ATIVA'}]
        @staticmethod
        def _kairos_select_entry_zone(*a,**kw):return Later._kairos_fvg_states([])[0]
    later=event(4*M15);later['close_ts']=4*M15+M5
    z,ready=chains._refinement(Later,bars(5*M15,M5),capture(),event(),dict(top=108,bottom=103),[later])
    assert z and z['id']=='valid' and ready==4*M15+M5
