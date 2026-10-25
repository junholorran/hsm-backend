import copy
import json
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import kairos_capture_chains as chains
from kairos_alert_presentation import format_chain_events


def ts(hour,minute=0):
    return int(datetime(2026,10,24,hour,minute,tzinfo=ZoneInfo('Europe/Lisbon')).timestamp()*1000)


def data(cutoff):
    raw=json.loads((Path(__file__).parent/'tests/fixtures/bnb_recent_ludo_20261025.json').read_text())
    return {tf:[c for c in raw['candles'][tf] if c['t']+d<=cutoff]
            for tf,d in [('M5',chains.M5),('M15',chains.M15)]}


def capture():
    return dict(liquidity_tf='H4',liquidity_type='EQL',nivel=724.32,
                liquidity_origin_ts=ts(0),first_capture_ts=ts(16),
                native_capture_confirm_ts=ts(16,15),liquidity_side='LOW')


def run(path,cutoff,monkeypatch):
    import scalp_engine as engine
    monkeypatch.setenv('KAIROS_ENTRY_STRUCTURE','LUDO')
    return chains.scan('BNBUSD',data(cutoff),cutoff,
                       {'structural_sweep_audit':{'candidates':[capture()]}},engine,str(path))


def outbox(path):
    with chains._db(str(path)) as db:
        return [json.loads(r[0]) for r in db.execute('SELECT payload FROM chain_outbox ORDER BY seq')]


def test_bnb_first_m5_notice_precedes_m15_and_does_not_consume_it(tmp_path,monkeypatch):
    path=tmp_path/'chains.db'
    for cutoff in [ts(15,55),ts(16,15),ts(20,30),ts(20,35),ts(22)]:
        run(path,cutoff,monkeypatch)
    events=outbox(path)
    early=[e for e in events if e['stage']=='M5_ANTECIPADO']
    assert len(early)==1
    assert early[0]['break_ts']==ts(20,35)
    assert early[0]['level']==729.8
    assert early[0]['direction']=='LONG'
    assert early[0]['entry_authorized'] is False
    later=[e for e in events if e['stage']=='M15_QUEBRA']
    assert [(e['break_ts'],e['level']) for e in later]==[(ts(22),732.3)]
    run(path,ts(22),monkeypatch)
    assert outbox(path)==events


def test_rollout_seeds_baseline_without_notifying_past_m5(tmp_path,monkeypatch):
    path=tmp_path/'chains.db'
    run(path,ts(15,55),monkeypatch)
    run(path,ts(16,15),monkeypatch)
    # Simulate state before this feature existed, with origin already tracked.
    with chains._db(str(path)) as db:
        r=json.loads(db.execute('SELECT payload FROM chain_state').fetchone()[0])
        r.pop('early_m5',None)
        db.execute('UPDATE chain_state SET payload=?',(json.dumps(r),))
    run(path,ts(20,35),monkeypatch)
    run(path,ts(20,40),monkeypatch)
    assert not any(e['stage']=='M5_ANTECIPADO' for e in outbox(path))


def test_m5_presentation_never_calls_it_m15_or_a_complete_entry():
    e=dict(pair='BNBUSD',capture_id='origin',stage='M5_ANTECIPADO',
           observed_ts=ts(20,35),break_ts=ts(20,35),kind='CHoCH',level=729.8,
           direction='LONG',capture=capture(),entry_authorized=False,
           reaction=dict(state='REJECTION_RECLAIM',confirmed_ts=ts(19,15),direction='LONG'))
    text='\n'.join(format_chain_events([e],now_ts=e['observed_ts']))
    assert 'CHoCH M5' in text
    assert 'M15 por confirmar' in text
    assert 'CONFIRMAÇÃO M15' not in text
    assert 'POI causal e reteste ainda por verificar' in text


def observer_state():
    return dict(captures={'origin':dict(capture=capture(),plan=None)},
                early_m5=dict(since=ts(16),asof_ts=ts(20,30),records={}))


def test_observer_is_pure_and_future_bars_do_not_create_early_break():
    from kairos_early_m5 import observe
    state=observer_state();before=copy.deepcopy(state)
    future=data(ts(22))
    unchanged,events=observe(state,'BNBUSD',future,ts(20,30),ts(20,30))
    assert events==[] and unchanged==state
    updated,events=observe(state,'BNBUSD',future,ts(20,35),ts(20,35))
    assert state==before
    assert [(e['break_ts'],e['direction']) for e in events]==[(ts(20,35),'LONG')]
    again,repeated=observe(updated,'BNBUSD',future,ts(20,40),ts(20,40))
    assert repeated==[]
    assert again['early_m5']['records']['origin']['break']['close_ts']==ts(20,35)


def test_unresolved_or_opposite_reaction_cannot_notify_long():
    from kairos_early_m5 import observe
    for close in (724.32,723):
        tf=data(ts(20,35))
        for c in tf['M15']:
            if c['t']>=ts(16):
                c.update(c=close,o=close,h=max(c['h'],close),l=min(c['l'],close))
        _,events=observe(observer_state(),'BNBUSD',tf,ts(20,35),ts(20,35))
        assert events==[]


def test_early_invalidation_does_not_consume_m15_episode():
    from kairos_early_m5 import observe
    state,events=observe(observer_state(),'BNBUSD',data(ts(20,35)),ts(20,35),ts(20,35))
    tf=data(ts(20,40));tf['M5'][-1]['l']=716
    updated,events=observe(state,'BNBUSD',tf,ts(20,40),ts(20,40))
    assert [e['stage'] for e in events]==['M5_ANTECIPADO_INVALIDADO']
    assert events[0]['reason']=='STRUCTURAL_EXTREME_BREACHED'
    assert events[0]['confirmation_ts']==ts(20,40)
    assert events[0]['invalidation_reasons'][0]['level']==717.23
    assert updated['captures']==state['captures']
    assert 'consumed' not in updated['captures']['origin']
    _,repeated=observe(updated,'BNBUSD',tf,ts(20,40),ts(20,40))
    assert repeated==[]


def test_late_m5_notice_is_explicitly_historical():
    from kairos_early_m5 import observe
    _,events=observe(observer_state(),'BNBUSD',data(ts(20,35)),ts(20,35),ts(21))
    text='\n'.join(format_chain_events(events,now_ts=ts(21)))
    assert 'Quebra M5 histórica' in text
    assert '20:35' in text and '21:00' in text


def test_gap_cannot_advance_early_watermark():
    from kairos_early_m5 import observe
    state=observer_state(); tf=data(ts(20,35));tf['M5'].pop(-2)
    updated,events=observe(state,'BNBUSD',tf,ts(20,35),ts(20,35))
    assert updated==state and events==[]


def test_confirmed_reaction_change_ends_early_hypothesis_at_its_own_clock():
    from kairos_early_m5 import observe
    state,_=observe(observer_state(),'BNBUSD',data(ts(20,35)),ts(20,35),ts(20,35))
    tf=data(ts(21))
    for c in tf['M15']:
        if c['t']>=ts(20,30):c.update(o=723,c=723,l=min(c['l'],723),h=max(c['h'],723))
    updated,events=observe(state,'BNBUSD',tf,ts(21),ts(21))
    assert len(events)==1
    assert events[0]['stage']=='M5_ANTECIPADO_INVALIDADO'
    assert events[0]['reason']=='CONFIRMED_REACTION_CHANGED'
    assert events[0]['confirmation_ts']==ts(21)
    assert updated['captures']==state['captures']


def test_future_swing_confirmation_cannot_create_notice(monkeypatch):
    import kairos_early_m5 as early
    from kairos_ludo_structure import structure_snapshot
    tf=data(ts(20,35))
    snap=structure_snapshot(tf['M5'],chains.M5,ts(20,35))
    for e in snap['events']:
        if e['close_ts']==ts(20,35):e['level_known_ts']=ts(21)
    monkeypatch.setattr(early,'structure_snapshot',lambda *args:snap)
    _,events=early.observe(observer_state(),'BNBUSD',tf,ts(20,35),ts(20,35))
    assert events==[]


def test_early_notice_uses_durable_retry_and_not_a_parallel_sender(tmp_path,monkeypatch):
    path=tmp_path/'chains.db'
    for cutoff in [ts(15,55),ts(16,15),ts(20,30),ts(20,35)]:run(path,cutoff,monkeypatch)
    failed=[];success=[]
    assert not chains.deliver('BNBUSD',lambda msg:failed.append(msg) or False,str(path))
    assert chains.deliver('BNBUSD',lambda msg:success.append(msg) or True,str(path))
    assert failed[0]==success[0]
    assert 'CHoCH M5' in '\n'.join(success)
    assert chains.deliver('BNBUSD',lambda _:(_ for _ in ()).throw(AssertionError('duplicate')),str(path))


def test_break_candle_that_breaches_its_opposite_extreme_cannot_start_live_hypothesis():
    from kairos_early_m5 import observe
    tf=data(ts(20,35));tf['M5'][-1]['l']=700
    updated,events=observe(observer_state(),'BNBUSD',tf,ts(20,35),ts(20,35))
    assert events==[]
    assert updated['early_m5']['records']=={}


def test_short_observation_is_symmetric_and_uses_native_closed_break():
    from kairos_early_m5 import observe
    tf=data(ts(20,35))
    for rows in tf.values():
        for c in rows:
            c.update(o=1500-c['o'],c=1500-c['c'],h=1500-c['l'],l=1500-c['h'])
    state=observer_state()
    state['captures']['origin']['capture'].update(liquidity_type='EQH',nivel=1500-724.32,liquidity_side='HIGH')
    _,events=observe(state,'BNBUSD',tf,ts(20,35),ts(20,35))
    assert [(e['break_ts'],e['direction']) for e in events]==[(ts(20,35),'SHORT')]
    assert abs(events[0]['level']-770.2)<1e-8
    assert events[0]['entry_authorized'] is False
