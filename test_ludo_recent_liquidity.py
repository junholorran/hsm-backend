"""First intact Ludo extreme -> reaction -> first opposite M15 break."""
import copy
import importlib
import json
import sqlite3
from types import SimpleNamespace
from pathlib import Path

import pytest
import scalp_engine as engine
import kairos_capture_chains as chains

DATA = json.loads((Path(__file__).parent/'tests/fixtures/bnb_recent_ludo_20261025.json').read_text())['candles']
START = 1792896300000  # 25 Oct 02:45 UTC, before first capture.
BREAK = 1792910700000  # 25 Oct 06:45 UTC.
DURATION = {'H4':14400000,'H1':3600000,'M15':900000,'M5':300000}


def detector():
    # Missing behavior must fail as an assertion before implementation.
    import importlib.util
    assert importlib.util.find_spec('kairos_ludo_liquidity'), 'Missing recent Ludo liquidity detector'
    return importlib.import_module('kairos_ludo_liquidity').captures


def prefix(cutoff):
    return {tf:[dict(c) for c in rows if c['t']+DURATION[tf]<=cutoff] for tf,rows in DATA.items()}


def target(captures, tf='M15'):
    return next(c for c in captures if c['liquidity_tf']==tf and c['nivel']==746.5
                and c['liquidity_side']=='HIGH')


def test_native_bnb_first_recent_capture_can_authorize_first_short_choch():
    find = detector()
    state, _ = chains.advance({},'BNBUSD',[],prefix(START),START,engine,observed_ts=START)
    alerts=[]
    for cutoff in range(START+300000,BREAK+1,300000):
        tf=prefix(cutoff)
        state,new=chains.advance(state,'BNBUSD',find(tf,cutoff),tf,cutoff,engine,observed_ts=cutoff)
        alerts.extend(new)
    cap=target(find(prefix(BREAK),BREAK))
    assert cap['liquidity_origin_ts']==1792895400000
    assert cap['liquidity_confirm_ts']<=1792900800000
    assert cap['first_capture_ts']==1792900800000
    assert cap['native_capture_confirm_ts']==1792901700000
    events=[a for a in alerts if a['capture_id']==chains.capture_id('BNBUSD',cap)]
    breaks=[a for a in events if a['stage']=='M15_QUEBRA']
    assert cap['liquidity_tf']=='M15'
    assert breaks==[]  # Local M15 liquidity no longer originates a trade.
    assert not any(a['stage']=='M5_ARMADO' for a in events)


def test_level_is_known_before_first_cross_and_is_not_recaptured():
    find=detector()
    assert not any(c['nivel']==746.5 for c in find(prefix(1792900800000),1792900800000))
    first=target(find(prefix(1792901700000),1792901700000))
    later=target(find(prefix(BREAK),BREAK))
    assert first==later
    assert first['liquidity_confirm_ts']<=first['first_capture_ts']
    assert first['origin_kind']=='LUDO_SWING_CAPTURE'


def test_future_candles_never_rewrite_captures_or_mutate_inputs():
    find=detector();source=copy.deepcopy(DATA);before=copy.deepcopy(source)
    result=find(source,BREAK)
    assert source==before
    for tf,rows in source.items():
        for c in rows:
            if c['t']+DURATION[tf]>BREAK:c.update(h=999999,l=1,c=999999)
    assert find(source,BREAK)==result


@pytest.mark.parametrize('tf',['M15','H1'])
def test_gap_between_level_and_capture_fails_closed(tf):
    find=detector();source=prefix(BREAK)
    source[tf]=[c for c in source[tf] if c['t']!=1792897200000]
    assert not any(c['liquidity_tf']==tf and c['nivel']==746.5 for c in find(source,BREAK))


def test_native_ludo_low_is_symmetric():
    find=detector();source=copy.deepcopy(prefix(BREAK));anchor=1500.
    for rows in source.values():
        for c in rows:c.update(o=anchor-c['o'],h=anchor-c['l'],l=anchor-c['h'],c=anchor-c['c'])
    lows=[c for c in find(source,BREAK) if c['liquidity_tf']=='M15'
          and c['liquidity_side']=='LOW' and c['nivel']==anchor-746.5]
    assert len(lows)==1 and lows[0]['first_capture_ts']==1792900800000


def test_equal_touch_is_not_a_capture():
    find=detector();source=prefix(1792901700000)
    bar=next(c for c in source['M15'] if c['t']==1792900800000)
    bar.update(h=746.5,c=746.4)
    assert not any(c['liquidity_tf']=='M15' and c['nivel']==746.5 for c in find(source,1792901700000))


def test_level_history_is_optional_and_does_not_change_structural_math():
    from kairos_ludo_structure import structure_snapshot
    plain=structure_snapshot(DATA['M15'],900000,BREAK)
    recorded=structure_snapshot(DATA['M15'],900000,BREAK,include_levels=True)
    assert recorded.pop('levels')
    assert recorded==plain


def test_native_closed_stream_rejects_nonfinite_values():
    find=detector();source=prefix(BREAK)
    source['M15'][-1]['h']=float('nan')
    assert find(source,BREAK)==[]


def read_state(path):
    with sqlite3.connect(path) as db:
        return json.loads(db.execute('SELECT payload FROM chain_state WHERE pair=?',('BNBUSD',)).fetchone()[0])


def test_live_scan_admits_fresh_ludo_capture_and_preserves_activation_on_restart(tmp_path,monkeypatch):
    monkeypatch.setenv('KAIROS_ENTRY_STRUCTURE','LUDO')
    find=detector();path=str(tmp_path/'chain.db')
    chains.scan('BNBUSD',prefix(START),START,{},engine,path)
    decision=dict(ludo_liquidity_origins=find(prefix(BREAK),BREAK))
    chains.scan('BNBUSD',prefix(BREAK),BREAK,decision,engine,path)
    state=read_state(path);cap=target(decision['ludo_liquidity_origins'])
    assert cap['liquidity_tf']=='M15'
    assert chains.capture_id('BNBUSD',cap) not in state['captures']
    assert state['ludo_liquidity_since']==START
    assert state['causal_policy_version']==2
    with sqlite3.connect(path) as db:
        before=db.execute('SELECT COUNT(*) FROM chain_outbox').fetchone()[0]
    chains.scan('BNBUSD',prefix(BREAK),BREAK,decision,engine,path)
    assert read_state(path)==state
    with sqlite3.connect(path) as db:
        assert db.execute('SELECT COUNT(*) FROM chain_outbox').fetchone()[0]==before


def test_rollout_never_adopts_pre_activation_recent_captures(tmp_path,monkeypatch):
    monkeypatch.setenv('KAIROS_ENTRY_STRUCTURE','LUDO')
    path=str(tmp_path/'chain.db');find=detector()
    # Existing v2 state from before this detector was installed.
    state=dict(start_ts=START,asof_ts=BREAK-300000,captures={},causal_policy_version=2,
               ludo_only_since=START)
    with chains._db(path) as db:
        db.execute('INSERT INTO chain_state VALUES (?,?)',('BNBUSD',json.dumps(state)))
    decision=dict(ludo_liquidity_origins=find(prefix(BREAK),BREAK))
    chains.scan('BNBUSD',prefix(BREAK),BREAK,decision,engine,path)
    saved=read_state(path)
    assert saved['ludo_liquidity_since']==BREAK
    assert saved['captures']=={}
    chains.scan('BNBUSD',prefix(BREAK+300000),BREAK+300000,decision,engine,path)
    assert read_state(path)['captures']=={}


def test_live_provider_feeds_labelled_recent_captures_to_durable_scan(monkeypatch):
    import kairos_ludo_entry as provider
    import kairos_daily_delivery as daily
    def scan(pair,tf,cutoff,decision,api):
        cap=target(decision['ludo_liquidity_origins'])
        assert cap['origin_kind']=='LUDO_SWING_CAPTURE'
        return dict(asof_ts=cutoff,view=dict(code='AGUARDANDO_CONFIRMACAO_M15'))
    fake=SimpleNamespace(_kairos_build_mtf_map=lambda tf:{},_kairos_context_bias=lambda tf:{},
        _kairos_select_structural_first_capture_sweep=lambda *a:(None,{'candidates':[]}),
        _kairos_capture_radar_events=lambda *a:[])
    monkeypatch.setattr(chains,'scan',scan)
    monkeypatch.setattr(daily,'scan_report',lambda *a:None)
    result=provider.snapshot('BNBUSD',prefix(BREAK),BREAK,fake)
    assert result['entry_structure_model']=='LUDO'
