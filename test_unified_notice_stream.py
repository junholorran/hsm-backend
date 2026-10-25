import copy
import html
import importlib.util
import json
import sqlite3
from unittest.mock import patch

import kairos_capture_chains as chains
from test_capture_chains import Engine,bars,M15,M5
from test_organized_alerts import proof,put


def api():
    assert importlib.util.find_spec('kairos_notice_context'), 'Missing unified notification context'
    return __import__('kairos_notice_context')


def radar(ts):
    return dict(key='reaction-'+str(ts),timestamp=ts,capture_confirm_ts=M15,
                liquidity_tf='H1',liquidity_type='SWING_LOW',level=80611.7,
                reaction='ACCEPTANCE_CONTINUATION',context={'D1':'alta','H4':'baixa','H1':'baixa'})


def poi(ts):
    e=proof('h4',stage='POI_HTF_TOCADO')
    e.update(observed_ts=ts,key='poi-'+str(ts),capture_id='fvg-h4')
    e['capture']['htf_location'].update(bottom=78470.,top=80535.,zone_id='btc-h4',tipo='FVG_bullish')
    e['capture']['native_capture_confirm_ts']=ts
    e.update(pair='BTCUSD');return e


def test_new_fvg_update_includes_previous_liquidity_with_own_confirmation_time():
    notice=api();r=radar(2*M15)
    cap=poi(3*M15)['capture']
    state=dict(asof_ts=3*M15,captures={'fvg-h4':dict(capture=cap,plan=None)},causal_policy_version=2)
    old=notice.context_snapshot({}, {}, [r],2*M15)
    context=notice.context_snapshot(old,state,[],3*M15)
    from kairos_alert_presentation import bundle_events
    groups=bundle_events([(1,poi(3*M15))])
    pages=notice.format_pair_update('BTCUSD',groups,context,now_ts=3*M15)
    text=html.unescape('\n'.join(pages))
    assert '80611,70' in text and '78470,00–80535,00' in text
    assert 'CONTEXTO DE LIQUIDEZ' in text and 'POIs HTF' in text
    assert 'aceitação / continuação' in text and 'sem captura de liquidez presumida' in text
    assert 'uma única cadeia' in text


def test_live_radar_goes_to_durable_outbox_not_parallel_sender(tmp_path,monkeypatch):
    monkeypatch.setenv('KAIROS_ENTRY_STRUCTURE','LUDO');path=str(tmp_path/'out.db')
    tf={'M15':bars(M15,M15),'M5':bars(M15,M5)}
    chains.scan('BTCUSD',tf,M15,{},Engine,path)
    tf={'M15':bars(2*M15,M15),'M5':bars(2*M15,M5)}
    with patch.object(chains,'structure_snapshot',return_value=dict(status='OK',events=[])):
        chains.scan('BTCUSD',tf,2*M15,dict(radar_context=[radar(2*M15)]),Engine,path)
    with sqlite3.connect(path) as db:
        events=[json.loads(x[0]) for x in db.execute('SELECT payload FROM chain_outbox')]
    assert len(events)==1 and events[0]['stage']=='RADAR_CONTEXT'
    assert events[0]['radar_event']['level']==80611.7
    with patch.object(chains,'structure_snapshot',return_value=dict(status='OK',events=[])):
        again=chains.scan('BTCUSD',tf,2*M15,dict(radar_context=[radar(2*M15)]),Engine,path)
    assert again['events']==0
    sent=[]
    assert chains.deliver('BTCUSD',lambda s:sent.append(s) or True,path)
    assert len(sent)==1 and '80611,70' in html.unescape(sent[0])


def test_rollout_seeds_context_without_resending_old_radar(tmp_path,monkeypatch):
    monkeypatch.setenv('KAIROS_ENTRY_STRUCTURE','LUDO');path=str(tmp_path/'out.db')
    tf={'M15':bars(3*M15,M15),'M5':bars(3*M15,M5)}
    chains.scan('BTCUSD',tf,3*M15,dict(radar_context=[radar(2*M15)]),Engine,path)
    with sqlite3.connect(path) as db:
        assert db.execute('SELECT COUNT(*) FROM chain_outbox').fetchone()[0]==0
        context=json.loads(db.execute('SELECT payload FROM chain_notice_context WHERE pair=?',('BTCUSD',)).fetchone()[0])
    assert context['liquidity']['H1']['level']==80611.7


def test_same_cycle_context_and_plan_send_one_update_but_keep_separate_sections(tmp_path,monkeypatch):
    monkeypatch.setenv('KAIROS_ENTRY_STRUCTURE','LUDO');path=str(tmp_path/'out.db')
    capture=proof(stage='CAPTURA');capture.pop('break_ts');capture['observed_ts']=1792875600100
    plan=proof();plan['observed_ts']=1792875600200
    put(path,[capture,plan]);sent=[]
    assert chains.deliver('BNBUSD',lambda s:sent.append(s) or True,path)
    assert len(sent)==1
    text=html.unescape(sent[0]);assert 'ATUALIZAÇÃO' in text and 'AGUARDAR RETESTE M5' in text
    with sqlite3.connect(path) as db:
        assert db.execute('SELECT SUM(ack) FROM chain_outbox').fetchone()[0]==2


def test_context_removes_invalid_fvg_and_does_not_move_backwards():
    notice=api();c=poi(2*M15)['capture']
    state=dict(asof_ts=2*M15,captures={'p':dict(capture=c,plan=None)},causal_policy_version=2)
    fresh=notice.context_snapshot({},state,[radar(2*M15)],2*M15)
    assert fresh['pois']
    state['captures']['p']['origin_invalidated']=True
    newest=notice.context_snapshot(fresh,state,[],3*M15)
    assert newest['pois']==[]
    assert notice.context_snapshot(newest,{},[radar(M15)],M15)==newest


def test_unified_pages_preserve_opposite_plan_sections_without_claiming_one_trade():
    notice=api();from kairos_alert_presentation import bundle_events
    a=proof();b=proof('h1',direction='SHORT')
    groups=bundle_events([(1,a),(2,b)])
    text=html.unescape('\n'.join(notice.format_pair_update('BNBUSD',groups,{},now_ts=a['observed_ts'])))
    assert 'PLANO DE COMPRA' in text and 'PLANO DE VENDA' in text
    assert 'uma única cadeia' in text


def test_provider_disables_only_parallel_sender_and_keeps_radar_audit(monkeypatch):
    from types import SimpleNamespace
    import kairos_ludo_entry as provider
    import kairos_daily_delivery as daily
    r=radar(2*M15)
    def save(pair,tf,cutoff,decision,engine):
        assert decision['radar_context']==[r]
        return dict(asof_ts=cutoff,view={'code':'AGUARDANDO_CONFIRMACAO_M15'})
    fake=SimpleNamespace(_kairos_build_mtf_map=lambda *a:{},_kairos_context_bias=lambda *a:{},
        _kairos_select_structural_first_capture_sweep=lambda *a:(None,{'candidates':[]}),
        _kairos_capture_radar_events=lambda *a:[copy.deepcopy(r)])
    monkeypatch.setattr(chains,'scan',save)
    monkeypatch.setattr(daily,'scan_report',lambda *a:None)
    tf={'M15':bars(2*M15,M15),'M5':bars(2*M15,M5)}
    result=provider.snapshot('BTCUSD',tf,2*M15,fake)
    assert result['radar_captures']==[]
    assert result['radar_context_events'][0]['level']==80611.7
    assert result['radar_context_events'][0]['entry_structure_model']=='LUDO'


def test_context_is_pure_and_keeps_future_radar_out():
    notice=api();previous={'asof_ts':M15,'liquidity':{},'pois':[]}
    before=copy.deepcopy(previous)
    result=notice.context_snapshot(previous,{},[radar(3*M15)],2*M15)
    assert previous==before and result['liquidity']=={}


def test_identical_radar_tuples_in_different_pairs_never_suppress_each_other(tmp_path,monkeypatch):
    monkeypatch.setenv('KAIROS_ENTRY_STRUCTURE','LUDO');path=str(tmp_path/'out.db')
    for pair in ['BTCUSD','BNBUSD']:
        tf={'M15':bars(M15,M15),'M5':bars(M15,M5)}
        chains.scan(pair,tf,M15,{},Engine,path)
        tf={'M15':bars(2*M15,M15),'M5':bars(2*M15,M5)}
        with patch.object(chains,'structure_snapshot',return_value=dict(status='OK',events=[])):
            saved=chains.scan(pair,tf,2*M15,dict(radar_context=[radar(2*M15)]),Engine,path)
        assert saved['events']==1
    with sqlite3.connect(path) as db:
        assert sorted(x[0] for x in db.execute('SELECT pair FROM chain_outbox'))==['BNBUSD','BTCUSD']
