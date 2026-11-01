import copy,html,os,sqlite3
from unittest.mock import patch
from kairos_notice_context import format_pair_update
from test_organized_alerts import proof


def render(e,context=None):
    return html.unescape('\n'.join(format_pair_update(e['pair'],[dict(seqs=[1],events=[e])],context or {},now_ts=e['observed_ts'])))


def test_capture_has_only_new_event_and_next_step_not_old_history():
    e=proof(stage='CAPTURA');original=copy.deepcopy(e)
    text=render(e,{'liquidity':{'D1':dict(level=999,timestamp=1)}})
    assert '💧 KAIROS | LIQUIDEZ CAPTURADA' in text
    assert 'H4 EQL' in text and '724,32' in text
    assert 'CONTEXTO DE LIQUIDEZ' not in text and '999' not in text
    assert 'Aguardar reação e quebra M15' in text and e==original
    assert len(text)<1000


def test_retest_has_plan_reference_and_never_claims_fill():
    e=proof(stage='RETESTE_OBSERVADO');e['candle_ts']=e['observed_ts']//300000*300000-300000
    text=render(e)
    assert 'RETESTE' in text and '🟢 COMPRA' in text
    assert '721,50–723,04' in text and 'Execução na Bybit não confirmada' in text
    assert 'CONTEXTO DE LIQUIDEZ' not in text and len(text)<1000


def test_cancel_shows_cause_and_does_not_cancel_exchange_orders():
    e=proof(stage='PLANO_INVALIDADO',direction='SHORT');e['reason']='PARENT_POI_INVALIDATED'
    text=render(e)
    assert '❌' in text and '🔴 VENDA' in text
    assert 'POI causal M15 invalidado' in text and 'não cancela ordens na Bybit' in text
    assert 'CONFIRMAÇÃO ORIGINAL DO PLANO' not in text


def test_four_hour_cadence_ignores_small_stage_changes_and_survives_restart(tmp_path,monkeypatch):
    import kairos_daily_delivery as d
    from test_daily_report_delivery import DailyIntegrationTests
    monkeypatch.setenv('KAIROS_DAILY_BRIEF_ENABLED','true')
    r=DailyIntegrationTests().report();db=str(tmp_path/'daily.db');sent=[]
    send=lambda s:sent.append(s) or True
    assert d.deliver(r,send,db);assert len(sent)==1
    r['asof_ts']+=300000;r['delivery_key']='new-stage'
    assert d.deliver(r,send,db);assert len(sent)==1
    r['asof_ts']+=14400000
    assert d.deliver(r,send,db);assert len(sent)==2
    assert d.deliver(copy.deepcopy(r),send,db);assert len(sent)==2
    assert 'PONTO DE SITUAÇÃO' in html.unescape(sent[-1])


def test_digest_shows_recent_changes_plans_and_native_zone_without_authorizing():
    from kairos_daily_report import notification_parts
    from test_daily_report_delivery import DailyIntegrationTests
    r=DailyIntegrationTests().report();r['asof_ts']+=14400000
    r['notice_summary']={'asof_ts':r['asof_ts'],'changes':[{'stage':'CAPTURA','known_ts':r['asof_ts']-300000,'capture':{'liquidity_tf':'D1','liquidity_type':'PDL','nivel':77871.2}}],
    'plans':[{'ref':'BTC-C1','direction':'LONG','code':'AGUARDANDO_RETESTE_M15','bottom':77949.9,'top':78122.5,'confirmed_ts':r['asof_ts']-600000}]}
    r['zones']=[dict(tf='H4',type='FVG_bearish',bottom=120,top=125,state='ATIVA',inside=False)]
    text='\n'.join(notification_parts(r))
    assert 'PONTO DE SITUAÇÃO' in text and 'Aconteceu' in text and 'Agora' in text and 'Esperar' in text
    assert 'BTC-C1' in text and '77949,90–78122,50' in text and 'H4' in text
    assert 'último M5 fechado' in text or 'referência' in text.lower()


def test_all_current_plans_are_projected_without_mutating_state():
    from kairos_notice_brief import state_summary
    import kairos_capture_chains as chains
    from test_capture_chains import advance,capture,event,M15
    state,_=advance({},[capture()],M15)
    state,_=advance(state,[capture()],2*M15)
    state,_=advance(state,[capture()],3*M15,[event()])
    state['pair']='BTCUSD';before=copy.deepcopy(state)
    result=state_summary(state,[],3*M15)
    assert result['plans'] and state==before
    for rec in state['captures'].values():
        if rec.get('plan'):rec['plan']['invalidated']=True
    assert state_summary(state,[],3*M15)['plans']==[]


def test_native_fvg_inventory_keeps_confluence_optional_and_does_not_mutate():
    import kairos_notice_brief as n
    assert hasattr(n,'fvg_inventory')
    cut=18000000
    z={'id':'x','tipo':'FVG_bullish','ludo_fvg':True,'state':'ATIVA','bottom':100,'top':110,'created_ts':0}
    maps={'H1':{'zones':[z]}}
    cs=[{'t':cut-300000,'o':111,'h':112,'l':109,'c':111}]
    levels=[dict(tf='H1',type='EQL',level=105,confirmed_ts=1000,state='ACTIVE')]
    frozen=copy.deepcopy((maps,levels,cs))
    out=n.fvg_inventory(maps,levels,[],cs,cut)
    assert out[0]['bottom']==100 and out[0]['liquidity_inside'][0]['level']==105
    assert n.fvg_inventory(maps,[],[],cs,cut)[0]['liquidity_inside']==[]
    assert (maps,levels,cs)==frozen


def test_half_delivered_digest_resumes_frozen_page_after_next_h4(tmp_path,monkeypatch):
    import kairos_daily_delivery as d
    from test_daily_report_delivery import DailyIntegrationTests
    monkeypatch.setenv('KAIROS_DAILY_BRIEF_ENABLED','true')
    monkeypatch.setattr(d,'notification_parts',lambda r:['first-'+str(r['asof_ts']),'second-'+str(r['asof_ts'])])
    r=DailyIntegrationTests().report();db=str(tmp_path/'daily.db');sent=[]
    def fail_second(s):sent.append(s);return len(sent)==1
    assert not d.deliver(r,fail_second,db)
    missing='second-'+str(r['asof_ts']);r['asof_ts']+=14400000;retry=[]
    assert d.deliver(r,lambda s:retry.append(s) or True,db)
    assert any(missing in s for s in retry)
    assert not any('first-'+str(r['asof_ts']-14400000) in s for s in retry)


def test_plan_references_distinguish_parent_and_match_projected_state():
    from kairos_notice_brief import plan_ref
    a=proof(stage='M15_ARMADO');b=copy.deepcopy(a);b['parent_zone']['bottom']-=1
    assert plan_ref(a)!=plan_ref(b)
    c=copy.deepcopy(a);c['stage']='RETESTE_OBSERVADO'
    assert plan_ref(a)==plan_ref(c)


def test_summary_failure_cannot_rollback_operational_state(tmp_path,monkeypatch):
    import kairos_capture_chains as c
    import kairos_notice_brief as n
    from test_capture_chains import Engine,bars,M15,M5
    monkeypatch.setenv('KAIROS_ENTRY_STRUCTURE','LUDO')
    monkeypatch.setattr(n,'state_summary',lambda *a:(_ for _ in ()).throw(RuntimeError('presentation')))
    path=str(tmp_path/'state.db');tm={'M15':bars(M15,M15),'M5':bars(M15,M5)}
    r=c.scan('BTCUSD',tm,M15,{},Engine,path)
    assert r['asof_ts']==M15 and r['notice_summary']=={}
    with sqlite3.connect(path) as db:assert db.execute('SELECT COUNT(*) FROM chain_state').fetchone()[0]==1


def test_inventory_failure_cannot_abort_native_provider(monkeypatch):
    from types import SimpleNamespace
    import kairos_notice_brief as n
    import kairos_ludo_entry as p
    import kairos_capture_chains as c
    import kairos_daily_delivery as d
    from test_capture_chains import bars,M15,M5
    monkeypatch.setattr(n,'fvg_inventory',lambda *a:(_ for _ in ()).throw(RuntimeError('presentation')))
    monkeypatch.setattr(c,'scan',lambda pair,tm,cut,*a:dict(asof_ts=cut,view={'code':'AGUARDANDO_CONFIRMACAO_M15'}))
    monkeypatch.setattr(d,'scan_report',lambda *a:None)
    engine=SimpleNamespace(_kairos_build_mtf_map=lambda *a:{},_kairos_context_bias=lambda *a:{},
        _kairos_select_structural_first_capture_sweep=lambda *a:(None,{'candidates':[]}),
        _kairos_capture_radar_events=lambda *a:[])
    result=p.snapshot('BTCUSD',{'M15':bars(M15,M15),'M5':bars(M15,M5)},M15,engine)
    assert result['ludo_entry_stage']['code']=='AGUARDANDO_CONFIRMACAO_M15'
    assert result['htf_fvg_touch_events']==[]


def test_capture_and_same_cycle_reaction_use_one_notice_without_losing_receipts():
    cap=proof(stage='CAPTURA');cap.pop('break_ts',None)
    rx=dict(pair=cap['pair'],stage='RADAR_CONTEXT',capture_id='radar',observed_ts=cap['observed_ts'],
       radar_event=dict(liquidity_tf='H4',liquidity_type='EQL',level=724.32,
           capture_confirm_ts=cap['capture']['native_capture_confirm_ts'],reaction='UNRESOLVED_REACTION'))
    groups=[dict(seqs=[1],events=[cap]),dict(seqs=[2],events=[rx])];frozen=copy.deepcopy(groups)
    pages=format_pair_update(cap['pair'],groups,{},now_ts=cap['observed_ts'])
    assert len(pages)==1 and 'LIQUIDEZ CAPTURADA' in pages[0] and groups==frozen
