import copy
import html
import importlib.util
import json
import sqlite3

import pytest
import kairos_capture_chains as chains
from test_capture_chains import Engine,bars,capture,event,M5,M15,advance


def ui():
    assert importlib.util.find_spec('kairos_alert_presentation'), 'Missing organized alert presentation'
    return __import__('kairos_alert_presentation')


def proof(cid='liquidity',stage='M5_ARMADO',direction='LONG'):
    cap=dict(liquidity_tf='H4',liquidity_type='EQL',liquidity_side='LOW',nivel=724.32,
             native_capture_confirm_ts=1792854900000)
    if cid!='liquidity':
        tf='H1' if cid=='h1' else 'H4'
        cap=dict(origin_kind='HTF_FVG_TOUCH',htf_location=dict(tf=tf,bottom=713.01,top=718.73),
                 native_capture_confirm_ts=1792855200000)
    return dict(key=cid+stage,pair='BNBUSD',capture_id=cid,capture=cap,stage=stage,
        observed_ts=1792875631115,causal_policy_version=2,direction=direction,
        break_ts=1792875600000,kind='CHoCH',level=732.3,invalidation=717.23,
        parent_zone=dict(tipo='FVG_bullish',bottom=721.5,top=723.04),
        zone=dict(tipo='FVG_bullish',lux_fvg=True,bottom=721.5,top=722.92),
        reaction=dict(state='REJECTION_RECLAIM',direction='LONG',confirmed_ts=1792865700000),
        plan_details=dict(m15_break=dict(kind='CHoCH',level=732.3,close_ts=1792875600000),
          parent_zone=dict(tipo='FVG_bullish',bottom=721.5,top=723.04),
          m5_zone=dict(tipo='FVG_bullish',lux_fvg=True,bottom=721.5,top=722.92),
          invalidation=717.23,had_retest=False))


def test_same_bnb_plan_groups_origins_and_m15_m5_proofs_without_mutation():
    api=ui();rows=[]
    for cid in ['liquidity','h1','h4']:
        rows.extend([(len(rows)+1,proof(cid,'M15_QUEBRA')),(len(rows)+2,proof(cid))])
    original=copy.deepcopy(rows);groups=api.bundle_events(rows)
    assert rows==original and len(groups)==1
    assert groups[0]['seqs']==[1,2,3,4,5,6]
    pages=api.format_chain_events(groups[0]['events'],now_ts=1792875640000)
    assert len(pages)==1
    text=html.unescape(pages[0])
    assert text.count('721,50–722,92')==1
    assert 'Liquidez H4 EQL' in text and 'POI H1 FVG' in text and 'POI H4 FVG' in text
    assert 'AGUARDAR RETESTE M5' in text
    assert 'Fundo estrutural abaixo da entrada: 717,23' in text
    assert 'Stop exato não calculado' in text
    assert 'Captura confirmada:' in text and 'Reação confirmada:' in text
    assert 'sem captura de liquidez presumida' in text


@pytest.mark.parametrize('change',[{'direction':'SHORT'},{'break_ts':1792876500000},
    {'zone':dict(tipo='FVG_bullish',bottom=722,top=723)}, {'observed_ts':1792875931115}])
def test_opposite_direction_different_zone_break_or_cycle_never_merge(change):
    api=ui();a=proof();b=proof('h1');b.update(change)
    assert len(api.bundle_events([(1,a),(2,b)]))==2


def test_m15_and_m5_of_opposite_directions_cannot_coalesce_even_with_same_origin():
    a=proof(stage='M15_QUEBRA');b=proof(direction='SHORT')
    assert len(ui().bundle_events([(1,a),(2,b)]))==2


def test_same_prices_do_not_merge_different_zone_identities():
    a=proof();b=proof('h1')
    a['parent_zone']['id']='parent-a';b['parent_zone']['id']='parent-b'
    a['zone']['id']='child-a';b['zone']['id']='child-b'
    assert len(ui().bundle_events([(1,a),(2,b)]))==2


def test_different_retest_candles_remain_distinct_during_catchup():
    a=proof(stage='RETESTE_OBSERVADO');b=proof('h1',stage='RETESTE_OBSERVADO')
    a['candle_ts']=a['break_ts']+M5;b['candle_ts']=a['candle_ts']+M5
    assert len(ui().bundle_events([(1,a),(2,b)]))==2


def test_reaction_invalidation_includes_new_reaction_confirmation():
    from test_ludo_causal_episodes import initial,run
    state,_=run(initial(),3*M15,events=[event()])
    rows=bars(5*M15,M15)
    for c in rows[3:]:c.update(o=109,h=111,l=108,c=110)
    _,alerts=run(state,5*M15,m15=rows,events=[event()])
    invalid=next(e for e in alerts if e['stage']=='PLANO_INVALIDADO')
    assert invalid['invalidation_reasons'][0]['confirmed_ts']==5*M15
    assert invalid['invalidation_reasons'][0]['level']==108
    assert invalid['new_reaction']['direction']=='LONG'


def test_short_reference_is_above_and_is_not_a_computed_stop():
    a=proof(direction='SHORT');a['invalidation']=748.63;a['plan_details']['invalidation']=748.63
    text=html.unescape(ui().format_chain_events([a],now_ts=a['observed_ts'])[0])
    assert 'Topo estrutural acima da entrada: 748,63' in text
    assert 'Stop exato não calculado' in text


def test_invalidation_has_reason_level_time_and_no_retest_status():
    a=proof(stage='PLANO_INVALIDADO');a.update(reason='OPPOSITE_M15_BREAK',
        invalidation_reasons=[dict(code='OPPOSITE_M15_BREAK',kind='CHoCH',level=743.63,
                                   close=743.32,confirmed_ts=1792910700000)])
    text=html.unescape(ui().format_chain_events([a],now_ts=a['observed_ts'])[0])
    assert 'PLANO DE COMPRA INVALIDADO' in text
    assert 'CHoCH contrário M15' in text and '743,63' in text and '06:45' in text
    assert 'sem reteste' in text
    assert 'não cancela ordens na exchange' in text


def test_partial_origin_invalidation_does_not_claim_all_plans_invalid():
    a=proof(stage='ORIGEM_INVALIDADA')
    b=proof('h1')
    groups=ui().bundle_events([(1,a),(2,b)])
    assert len(groups)==2
    msg=html.unescape(ui().format_chain_events(groups[0]['events'],now_ts=a['observed_ts'])[0])
    assert 'desta origem' in msg


def test_all_pages_are_safe_bounded_and_keep_all_origins():
    api=ui();events=[proof('h1') for _ in range(55)]
    for i,e in enumerate(events):e['capture_id']=str(i);e['capture']['htf_location']['tf']='<H1&'+str(i)+'>'
    pages=api.format_chain_events(events,now_ts=events[0]['observed_ts'])
    assert len(pages)>1
    assert all(len(p.encode('utf-16-le'))//2<=3900 for p in pages)
    assert all('<H1&' not in p for p in pages)
    text=html.unescape('\n'.join(pages))
    assert all('<H1&'+str(i)+'>' in text for i in range(55))


def test_retest_is_observed_not_fill_and_delayed_delivery_is_clear():
    a=proof(stage='RETESTE_OBSERVADO');a['candle_ts']=a['break_ts']+M5
    text=html.unescape(ui().format_chain_events([a],now_ts=a['observed_ts']+2*M15)[0])
    assert 'RETESTE OBSERVADO' in text and 'execução não comprovada' in text
    assert 'histórico' in text.lower()


def test_existing_invalidation_emits_exact_facts_without_changing_transition():
    state,_=advance({},[capture()],M15)
    state,_=advance(state,[capture()],2*M15)
    state,_=advance(state,[capture()],3*M15,[event()])
    _,alerts=advance(state,[capture()],4*M15,[event(),event(3*M15,'UP')])
    invalid=next(a for a in alerts if a['stage']=='PLANO_INVALIDADO')
    assert invalid['reason']=='OPPOSITE_M15_BREAK'
    assert invalid['invalidation_reasons'][0]['level']==102
    assert invalid['plan_details']['m5_zone']['bottom']==104


def put(path,rows):
    with chains._db(path) as db:
        for e in copy.deepcopy(rows):
            # Delivery fixtures use eligible HTF liquidity, while formatter-only
            # tests above still exercise standalone POI context presentation.
            if e.get('stage') in ('M15_QUEBRA','M5_ARMADO','RETESTE_OBSERVADO') and e['capture'].get('origin_kind')=='HTF_FVG_TOUCH':
                e['capture'].pop('origin_kind')
                e['capture'].update(liquidity_tf='H1',liquidity_type='EQL',liquidity_side='LOW',nivel=724.32)
            db.execute('INSERT INTO chain_outbox(key,pair,payload) VALUES(?,?,?)',
                       (e['key'],'BNBUSD',json.dumps(e)))


def test_bundle_delivery_retries_pages_without_consuming_events_early(tmp_path,monkeypatch):
    monkeypatch.setenv('KAIROS_ENTRY_STRUCTURE','LUDO');path=str(tmp_path/'out.db')
    rows=[proof('h1'+str(i)) for i in range(45)]
    for i,e in enumerate(rows):e['capture']['htf_location']['tf']='H1'+str(i)
    put(path,rows);sent=[]
    def sender(text):sent.append(text);return len(sent)==1
    assert chains.deliver('BNBUSD',sender,path) is False
    with sqlite3.connect(path) as db:
        assert db.execute('SELECT SUM(ack) FROM chain_outbox').fetchone()[0]==0
    first=sent[0];sent=[]
    assert chains.deliver('BNBUSD',lambda s:sent.append(s) or True,path) is True
    assert first not in sent
    with sqlite3.connect(path) as db:
        assert db.execute('SELECT COUNT(*) FROM chain_outbox WHERE ack=1').fetchone()[0]==45


def test_six_source_events_send_one_notice_and_preserve_receipts(tmp_path,monkeypatch):
    monkeypatch.setenv('KAIROS_ENTRY_STRUCTURE','LUDO');path=str(tmp_path/'out.db')
    put(path,[proof(cid,stage) for cid in ['liquidity','h1','h4'] for stage in ['M15_QUEBRA','M5_ARMADO']])
    sent=[]
    assert chains.deliver('BNBUSD',lambda s:sent.append(s) or True,path)
    assert len(sent)==1
    assert chains.deliver('BNBUSD',lambda s:sent.append(s) or True,path)
    assert len(sent)==1


def test_new_event_during_retry_does_not_rebundle_the_frozen_notice(tmp_path,monkeypatch):
    monkeypatch.setenv('KAIROS_ENTRY_STRUCTURE','LUDO');path=str(tmp_path/'out.db')
    put(path,[proof('liquidity')]);sent=[]
    assert not chains.deliver('BNBUSD',lambda s:sent.append(s) or False,path)
    frozen=sent[0]
    put(path,[proof('h1')])
    sent=[]
    assert chains.deliver('BNBUSD',lambda s:sent.append(s) or True,path)
    assert sent[0]==frozen and len(sent)==2
    with sqlite3.connect(path) as db:
        assert db.execute('SELECT COUNT(*) FROM chain_delivery_batches WHERE complete=1').fetchone()[0]==2
    assert chains.deliver('BNBUSD',lambda s:sent.append(s) or True,path)
    assert len(sent)==2


def test_live_capture_and_htf_touch_share_layout_without_mixing_origins(monkeypatch):
    monkeypatch.setenv('KAIROS_ENTRY_STRUCTURE','LUDO')
    from kairos_telegram_ui import format_capture_message
    from kairos_htf_fvg_alerts import format_fvg_touch_message
    capture_msg=html.unescape(format_capture_message('BNBUSD',dict(liquidity_tf='H1',
        liquidity_type='SWING_HIGH',level=746.5,timestamp=1792901700000,
        capture_confirm_ts=1792901700000,reaction='UNRESOLVED_REACTION',context={'H1':'alta'})))
    assert 'ORIGENS IDENTIFICADAS' in capture_msg and 'ESTADO / PRÓXIMO PASSO' in capture_msg
    assert '746,50' in capture_msg and 'M15' in capture_msg and 'Ludo' in capture_msg
    poi_msg=html.unescape(format_fvg_touch_message('BNBUSD',dict(tf='H1',tipo='FVG_bullish',
        bottom=713.01,top=718.73,touch_close_ts=1792855200000)))
    assert 'ORIGENS IDENTIFICADAS' in poi_msg and 'sem captura de liquidez presumida' in poi_msg
    assert '713,01–718,73' in poi_msg and 'SEM TRADE' in poi_msg


def test_retry_after_delay_marks_frozen_actionable_notice_as_historical(tmp_path,monkeypatch):
    monkeypatch.setenv('KAIROS_ENTRY_STRUCTURE','LUDO');path=str(tmp_path/'out.db')
    e=proof();clock=[e['observed_ts']/1000]
    monkeypatch.setattr(chains.time,'time',lambda:clock[0])
    put(path,[e]);sent=[]
    assert not chains.deliver('BNBUSD',lambda s:sent.append(s) or False,path)
    assert 'histórico' not in sent[0]
    clock[0]+=2*M15/1000;sent=[]
    assert chains.deliver('BNBUSD',lambda s:sent.append(s) or True,path)
    assert 'histórico' in sent[0] and 'entrega atrasada' in sent[0].lower()


def test_legacy_pending_m15_and_m5_keep_original_confirmation_after_grouping():
    a=proof(stage='M15_QUEBRA');b=proof()
    for e in [a,b]:e.pop('plan_details')
    b.pop('kind');b.pop('level')
    group=ui().bundle_events([(1,a),(2,b)])[0]
    text=html.unescape(ui().format_chain_events(group['events'],now_ts=b['observed_ts'])[0])
    assert 'CHoCH M15' in text and '732,30' in text


def test_frozen_batch_from_an_old_policy_is_suppressed_after_migration(tmp_path,monkeypatch):
    monkeypatch.setenv('KAIROS_ENTRY_STRUCTURE','LUDO');path=str(tmp_path/'out.db')
    put(path,[proof()])
    assert not chains.deliver('BNBUSD',lambda s:False,path)
    monkeypatch.setattr(chains,'CAUSAL_POLICY_VERSION',3)
    sent=[]
    assert chains.deliver('BNBUSD',lambda s:sent.append(s) or True,path)
    assert sent==[]
    with sqlite3.connect(path) as db:
        assert db.execute('SELECT ack FROM chain_outbox').fetchone()[0]==2
