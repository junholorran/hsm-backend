import copy
import json
from unittest.mock import patch
import pytest
import kairos_capture_chains as chains
from test_capture_chains import Engine,bars,capture,event,M5,M15
from test_ludo_causal_episodes import initial,run

@pytest.mark.parametrize('side,direction',[('HIGH','UP'),('LOW','DOWN')])
def test_acceptance_cannot_create_m15_entry_but_reaction_remains_context(side,direction):
    cap={**capture(),'liquidity_side':side}
    rows=bars(3*M15,M15)
    price=110 if side=='HIGH' else 100
    for c in rows[1:]:c.update(o=price,h=price+1,l=price-1,c=price)
    br=event(side=direction);br['opposite_level']=90 if direction=='UP' else 120
    state,_=run({},M15,captures=[cap])
    state,_=run(state,2*M15,captures=[cap],m15=rows[:2])
    state,alerts=run(state,3*M15,m15=rows,captures=[cap],events=[br])
    assert not any(a['stage'] in ('M15_QUEBRA','M5_ARMADO','RETESTE_OBSERVADO') for a in alerts)
    rec=state['captures'][chains.capture_id('SOLUSD',cap)]
    assert rec['reaction']['state']=='ACCEPTANCE_CONTINUATION'
    assert rec['plan'] is None


def acceptance_plan():
    s,_=run(initial(),3*M15,events=[event()])
    rec=next(iter(s['captures'].values()))
    rec['plan']['break']['side']='UP'
    rec['plan']['break']['opposite_level']=90
    rec['plan']['invalidation']=90
    rec['reaction'].update(state='ACCEPTANCE_CONTINUATION',direction='LONG')
    rec['plan']['touched']=False
    return s


def test_existing_acceptance_plan_is_cancelled_once_and_not_retested():
    state=acceptance_plan()
    rows=bars(4*M15,M15)
    for c in rows[1:]:c.update(o=110,h=111,l=109,c=110)
    updated,alerts=run(state,4*M15,m15=rows)
    policy=[a for a in alerts if a.get('reason')=='ENTRY_POLICY_CHANGED']
    assert len(policy)==1
    assert not any(a['stage']=='RETESTE_OBSERVADO' for a in alerts)
    plan=next(iter(updated['captures'].values())).get('plan')
    assert plan is None or plan['invalidated']
    _,again=run(updated,4*M15,m15=rows)
    assert again==[]


def test_state_view_never_advertises_old_acceptance_plan():
    assert chains.state_view(acceptance_plan())['setup'] is None


def test_queued_acceptance_plan_is_suppressed_but_capture_kept(tmp_path,monkeypatch):
    monkeypatch.setenv('KAIROS_ENTRY_STRUCTURE','LUDO')
    path=str(tmp_path/'outbox.db')
    _,events=run(initial(),3*M15,events=[event()])
    e=next(a for a in events if a['stage']=='M5_ARMADO')
    e.update(direction='LONG',reaction=dict(state='ACCEPTANCE_CONTINUATION',direction='LONG'))
    with chains._db(path) as db:
        db.execute('INSERT INTO chain_outbox(key,pair,payload) VALUES(?,?,?)',(e['key'],'SOLUSD',json.dumps(e)))
    sent=[]
    chains.deliver('SOLUSD',lambda s:sent.append(s) or True,path)
    assert sent==[]
    with chains._db(path) as db:
        assert db.execute('SELECT ack FROM chain_outbox').fetchone()[0]==2


def test_early_m5_does_not_emit_acceptance_as_buy():
    import kairos_early_m5 as early
    from test_early_m5_notices import observer_state,data,ts
    state=observer_state()
    cap=state['captures']['origin']['capture']
    cap.update(liquidity_side='HIGH',nivel=724.32)
    with patch.object(early,'reaction_timeline',return_value=[dict(state='ACCEPTANCE_CONTINUATION',direction='LONG',confirmed_ts=ts(19,15),key='accept')]):
        _,events=early.observe(state,'BNBUSD',data(ts(20,35)),ts(20,35),ts(20,35))
    assert not any(a['stage']=='M5_ANTECIPADO' for a in events)


def test_native_sol_1245_acceptance_retest_is_blocked():
    from pathlib import Path
    import scalp_engine as engine
    f=json.loads((Path(__file__).parent/'tests/fixtures/sol_acceptance_20261026.json').read_text())
    start=1793007900000  # 26 Oct 09:45 UTC; Lisbon UTC on this date.
    state={};notices=[]
    for cutoff in range(start,f['cutoff_ts']+1,M5):
        tf={k:[c for c in cs if c['t']+d<=cutoff] for k,d in [('M5',M5),('M15',M15)] for cs in [f['candles'][k]]}
        state,events=chains.advance(state,'SOLUSD',[f['capture']],tf,cutoff,engine,observed_ts=cutoff+16000)
        notices.extend(events)
    assert not any(e.get('direction')=='LONG' and e['stage'] in ('M15_QUEBRA','M5_ARMADO','RETESTE_OBSERVADO') for e in notices)
    assert f['capture']['liquidity_tf']=='M15'
    assert chains.capture_id('SOLUSD',f['capture']) not in state['captures']
    assert not notices  # This local origin is excluded in both directions.


def test_acceptance_cannot_rearm_but_later_rejection_can():
    s,events=run(initial(),3*M15,events=[event()])
    rows=bars(7*M15,M15)
    for c in rows[3:5]:c.update(o=110,h=111,l=109,c=110)
    up=event(4*M15,'UP');up['opposite_level']=90
    s,events=run(s,5*M15,m15=rows[:5],events=[event(),up])
    assert not any(e['stage']=='M15_QUEBRA' for e in events)
    _,events=run(s,7*M15,m15=rows,events=[event(),up,event(6*M15)])
    assert any(e['stage']=='M15_QUEBRA' and e['direction']=='SHORT' for e in events)


def test_existing_early_acceptance_hypothesis_ends_once():
    import kairos_early_m5 as early
    from test_early_m5_notices import observer_state,data,ts
    s=observer_state()
    s,events=early.observe(s,'BNBUSD',data(ts(20,35)),ts(20,35),ts(20,35))
    s['captures']['origin']['capture']['liquidity_side']='HIGH'
    s,events=early.observe(s,'BNBUSD',data(ts(20,40)),ts(20,40),ts(20,40))
    assert [(e['stage'],e['reason']) for e in events]==[('M5_ANTECIPADO_INVALIDADO','ENTRY_POLICY_CHANGED')]
    _,events=early.observe(s,'BNBUSD',data(ts(20,45)),ts(20,45),ts(20,45))
    assert events==[]


def test_frozen_retry_acceptance_batch_is_not_sent_after_policy_change(tmp_path,monkeypatch):
    monkeypatch.setenv('KAIROS_ENTRY_STRUCTURE','LUDO')
    path=str(tmp_path/'outbox.db')
    _,events=run(initial(),3*M15,events=[event()])
    e=next(a for a in events if a['stage']=='M5_ARMADO')
    # Freeze a pre-upgrade page through the production delivery mechanism.
    with chains._db(path) as db:
        db.execute('INSERT INTO chain_outbox(key,pair,payload) VALUES(?,?,?)',(e['key'],'SOLUSD',json.dumps(e)))
    assert not chains.deliver('SOLUSD',lambda _:False,path)
    e.update(direction='LONG',reaction=dict(state='ACCEPTANCE_CONTINUATION',direction='LONG'))
    with chains._db(path) as db:
        db.execute('UPDATE chain_outbox SET payload=?',(json.dumps(e),))
    sent=[]
    assert chains.deliver('SOLUSD',lambda s:sent.append(s) or True,path)
    assert sent==[]
    with chains._db(path) as db:
        assert db.execute('SELECT complete FROM chain_delivery_batches').fetchone()[0]==1
        assert db.execute('SELECT ack FROM chain_outbox').fetchone()[0]==2


def test_acceptance_radar_is_kept_and_explicitly_context_only():
    from kairos_notice_context import _liquidity_lines
    text='\n'.join(_liquidity_lines(dict(liquidity_tf='M15',liquidity_type='LUDO_SWING_HIGH',level=101.54,reaction='ACCEPTANCE_CONTINUATION',timestamp=1793016900000)))
    assert 'aceitação / continuação' in text
    assert 'não autoriza' in text
    assert 'aguardar autorização estrutural' not in text


def test_partial_mixed_frozen_batch_preserves_receipt_without_replay(tmp_path,monkeypatch):
    monkeypatch.setenv('KAIROS_ENTRY_STRUCTURE','LUDO')
    path=str(tmp_path/'outbox.db')
    assert chains.deliver('SOLUSD',lambda _:True,path)  # Create delivery schema.
    _,events=run(initial(),3*M15,events=[event()])
    good=next(a for a in events if a['stage']=='M5_ARMADO')
    bad=copy.deepcopy(good);bad.update(key='bad',direction='LONG')
    with chains._db(path) as db:
        for e in (good,bad):
            db.execute('INSERT INTO chain_outbox(key,pair,payload) VALUES(?,?,?)',(e['key'],'SOLUSD',json.dumps(e)))
        seqs=[r[0] for r in db.execute('SELECT seq FROM chain_outbox ORDER BY seq')]
        db.execute('INSERT INTO chain_delivery_batches(key,pair,pages,seqs,acknowledged,observed_ts,policy_version) VALUES(?,?,?,?,?,?,?)',
                   ('mixed','SOLUSD',json.dumps(['already delivered','pending unsafe']),json.dumps(seqs),1,good['observed_ts'],chains.CAUSAL_POLICY_VERSION))
    sent=[]
    assert chains.deliver('SOLUSD',lambda s:sent.append(s) or True,path)
    assert sent==[]
    with chains._db(path) as db:
        assert db.execute('SELECT acknowledged,complete FROM chain_delivery_batches WHERE key=?',('mixed',)).fetchone()==(1,1)
        assert [r[0] for r in db.execute('SELECT ack FROM chain_outbox ORDER BY seq')]==[2,2]
        good['key']='later-valid'
        db.execute('INSERT INTO chain_outbox(key,pair,payload) VALUES(?,?,?)',(good['key'],'SOLUSD',json.dumps(good)))
    assert chains.deliver('SOLUSD',lambda s:sent.append(s) or True,path)
    assert len(sent)==1 and 'AGUARDAR RETESTE M5' in sent[0]
    assert chains.deliver('SOLUSD',lambda _:pytest.fail('duplicate delivery'),path)


def test_low_acceptance_early_short_and_persisted_short_are_blocked():
    import kairos_early_m5 as early
    from test_early_m5_notices import observer_state,data,ts
    tf=data(ts(20,35));s=observer_state()
    for rows in tf.values():
        for c in rows:c.update(o=1500-c['o'],c=1500-c['c'],h=1500-c['l'],l=1500-c['h'])
    s['captures']['origin']['capture'].update(liquidity_side='LOW',nivel=1500-724.32)
    with patch.object(early,'reaction_timeline',return_value=[dict(state='ACCEPTANCE_CONTINUATION',direction='SHORT',confirmed_ts=ts(19,15),key='accept')]):
        _,events=early.observe(s,'BNBUSD',tf,ts(20,35),ts(20,35))
    assert not any(a['stage']=='M5_ANTECIPADO' for a in events)
    s=acceptance_plan();rec=next(iter(s['captures'].values()))
    rec['capture']['liquidity_side']='LOW'
    rec['plan']['break']['side']='DOWN'
    rec['reaction']['direction']='SHORT'
    assert chains.state_view(s)['setup'] is None
    _,events=run(s,4*M15,captures=[rec['capture']])
    assert sum(a.get('reason')=='ENTRY_POLICY_CHANGED' for a in events)==1
