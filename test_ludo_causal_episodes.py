"""Regressions for false attribution of later breaks to old captures."""
import copy
from unittest.mock import patch

import kairos_capture_chains as chains
from test_capture_chains import Engine, bars, capture, event, M15, M5


def run(state, cutoff, m15=None, events=(), captures=None, m5=None):
    tf={'M15':m15 or bars(cutoff,M15),'M5':m5 or bars(cutoff,M5)}
    with patch.object(chains,'structure_snapshot',return_value=dict(status='OK',events=list(events))):
        return chains.advance(state,'SOLUSD',captures or [capture()],tf,cutoff,Engine)


def initial():
    state,_=run({},M15,captures=[])
    state,_=run(state,2*M15)
    return state


def test_unresolved_reaction_cannot_arm_m15_or_m5():
    rows=bars(3*M15,M15)
    rows[1].update(o=109,h=110,l=108,c=109)
    state,alerts=run(initial(),3*M15,m15=rows,events=[event()])
    assert not any(e['stage'] in ('M15_QUEBRA','M5_ARMADO') for e in alerts)
    assert chains.state_view(state)['code']=='AGUARDANDO_REACAO_CONFIRMADA'


def test_confirmed_acceptance_above_high_cannot_authorize_short():
    rows=bars(3*M15,M15)
    for c in rows[1:]:c.update(o=109,h=111,l=108,c=110)
    _,alerts=run(initial(),3*M15,m15=rows,events=[event()])
    assert not any(e['stage'] in ('M15_QUEBRA','M5_ARMADO') for e in alerts)


def test_actual_reaction_updates_in_state_and_in_actionable_proof():
    state,alerts=run(initial(),3*M15,events=[event()])
    rec=next(iter(state['captures'].values()))
    assert rec['reaction']['state']=='REJECTION_RECLAIM'
    armed=next(e for e in alerts if e['stage']=='M5_ARMADO')
    assert armed['reaction']['confirmed_ts']==3*M15
    assert armed['reaction']['direction']=='SHORT'


def test_invalidated_plan_cannot_rearm_from_same_reaction_episode():
    state,_=run(initial(),3*M15,events=[event()])
    rows=bars(3*M15,M5)+[dict(t=3*M15,o=103,h=112,l=102,c=111)]
    state,_=run(state,3*M15+M5,events=[event()],m5=rows)
    _,alerts=run(state,5*M15,events=[event(),event(4*M15)],m5=rows+bars(5*M15,M5)[10:])
    assert not any(e['stage'] in ('M15_QUEBRA','M5_ARMADO') for e in alerts)


def test_future_reaction_cannot_authorize_an_earlier_break_in_catchup():
    rows=bars(5*M15,M15)
    rows[1].update(o=109,h=110,l=108,c=109)
    # At the 3*M15 break the adjacent closes straddle the captured high.
    # Only 4*M15 confirms two closes below; it must not bless the earlier break.
    _,alerts=run(initial(),5*M15,m15=rows,events=[event()])
    assert not any(e['stage']=='M5_ARMADO' for e in alerts)


def test_new_confirmed_reaction_is_a_new_episode_not_a_new_capture():
    state,_=run(initial(),3*M15,events=[event()])
    rows=bars(5*M15,M15)
    for c in rows[3:]:c.update(o=109,h=111,l=108,c=110)
    up=event(4*M15,'UP');up['opposite_level']=90
    state,alerts=run(state,5*M15,m15=rows,events=[event(),up])
    assert not any(e['stage']=='M15_QUEBRA' for e in alerts)
    rec=next(iter(state['captures'].values()))
    assert rec['reaction']['state']=='ACCEPTANCE_CONTINUATION'
    assert rec['reaction']['confirmed_ts']==5*M15
    assert rec['capture']['native_capture_confirm_ts']==2*M15
    assert not any(e['stage']=='CAPTURA' for e in alerts)


def test_newer_invalid_origin_does_not_hide_an_active_plan():
    state,_=run(initial(),3*M15,events=[event()])
    state['captures']['dead']=dict(capture={**capture(), 'native_capture_confirm_ts':4*M15},origin_invalidated=True,plan=None)
    assert chains.state_view(state)['code']=='AGUARDANDO_RETESTE_M5'


def test_observation_message_contains_capture_and_reaction_confirmation():
    _,alerts=run(initial(),3*M15,events=[event()])
    msg=chains.message(next(e for e in alerts if e['stage']=='M15_QUEBRA'))
    assert 'Captura confirmada:' in msg
    assert 'Reação confirmada:' in msg


def test_reaction_leg_keeps_the_extreme_before_the_two_confirming_closes():
    from kairos_reaction_episode import reaction_timeline
    rows=bars(5*M15,M15)
    rows[1].update(o=109,h=110,l=108,c=109)
    rows[2].update(o=109,h=115,l=108,c=110)
    rows[3].update(o=110,h=112,l=100,c=101)
    rows[4].update(o=101,h=102,l=99,c=100)
    changes=reaction_timeline(rows,capture())
    assert changes[-1]['confirmed_ts']==5*M15
    # The bearish displacement can start at the high in bar 2, before both
    # below-level closes. The original Ludo pivot further clamps the POI leg.
    assert changes[-1]['leg_start_ts']==2*M15


def test_unsafe_persisted_state_is_rebased_without_rearming_old_capture():
    state,_=run(initial(),3*M15,events=[event()])
    state.pop('causal_policy_version',None)
    updated,alerts=run(state,4*M15,events=[event(),event(3*M15)])
    assert updated['start_ts']==4*M15
    assert updated['captures']=={}
    assert not any(e['stage']=='M5_ARMADO' for e in alerts)
    assert updated['causal_policy_version']==chains.CAUSAL_POLICY_VERSION


def test_pending_unsafe_alert_is_suppressed_without_deleting_audit(tmp_path,monkeypatch):
    import sqlite3,json
    monkeypatch.setenv('KAIROS_ENTRY_STRUCTURE','LUDO')
    path=str(tmp_path/'outbox.db')
    _,alerts=run(initial(),3*M15,events=[event()])
    old=next(e for e in alerts if e['stage']=='M5_ARMADO')
    old.pop('causal_policy_version',None)
    with chains._db(path) as db:
        db.execute('insert into chain_outbox (key,pair,payload) values (?,?,?)',('unsafe','SOLUSD',json.dumps(old)))
    sent=[]
    assert chains.deliver('SOLUSD',lambda msg:sent.append(msg) or True,path)
    assert not sent
    with sqlite3.connect(path) as db:
        assert db.execute('select count(*) from chain_outbox').fetchone()[0]==1
        assert db.execute('select ack from chain_outbox').fetchone()[0]!=0


def test_reaction_gaps_and_equal_closes_do_not_create_confirmation():
    from kairos_reaction_episode import reaction_timeline
    rows=bars(4*M15,M15)
    rows[1]['c']=108
    assert reaction_timeline(rows[:3],capture())==[]
    assert reaction_timeline([rows[1],rows[3]],capture())==[]


def test_upcoming_reaction_change_cannot_erase_earlier_retest_in_catchup():
    state,_=run(initial(),3*M15,events=[event()])
    m15=bars(5*M15,M15)
    for c in m15[3:]:c.update(o=109,h=111,l=108,c=110)
    m5=bars(5*M15,M5)
    m5[9]=dict(t=3*M15,o=103,h=104.5,l=103,c=104)
    _,alerts=run(state,5*M15,m15=m15,m5=m5,events=[event()])
    retest=next(e for e in alerts if e['stage']=='RETESTE_OBSERVADO')
    invalid=next(e for e in alerts if e['stage']=='PLANO_INVALIDADO')
    assert retest['candle_ts']==3*M15
    assert invalid['candle_ts']+M5==5*M15


def test_fvg_touch_without_native_direction_fails_closed():
    origin=dict(origin_kind='HTF_FVG_TOUCH',origin_key='H1|unknown',
        htf_location=dict(tf='H1',zone_id='unknown',bottom=103,top=109),
        first_capture_ts=M15,native_capture_confirm_ts=2*M15)
    state,_=run({},M15)
    state,_=run(state,2*M15,captures=[origin])
    _,alerts=run(state,3*M15,captures=[origin],events=[event()])
    assert not any(e['stage']=='M5_ARMADO' for e in alerts)


def test_migration_does_not_confirm_a_cutoff_with_missing_candles():
    state,_=run(initial(),3*M15,events=[event()])
    state.pop('causal_policy_version')
    incomplete=bars(4*M15,M5);incomplete.pop(2)
    updated,alerts=run(state,4*M15,events=[event()],m5=incomplete)
    assert updated==state and alerts==[]
    assert chains.state_view(updated)['setup'] is None


def test_migration_cannot_move_watermark_backwards():
    state,_=run(initial(),3*M15,events=[event()])
    state.pop('causal_policy_version')
    updated,_=run(state,2*M15,events=[])
    assert updated==state


def test_missing_capture_bar_cannot_preserve_stale_wrong_side_reaction():
    state,_=run(initial(),3*M15)
    rows=bars(6*M15,M15)
    for c in rows[3:5]:c.update(o=109,h=110,l=108,c=109)
    # Full history would now say LONG acceptance; a single later below close
    # has not confirmed a SHORT rejection. Dropping capture's first bar must
    # not fall back to the previously stored short reaction.
    updated,alerts=run(state,6*M15,m15=rows[2:],events=[event(5*M15)])
    assert not any(e['stage']=='M5_ARMADO' for e in alerts)
    assert next(iter(updated['captures'].values()))['history_lost']


def test_native_btc_consumed_acceptance_cannot_rearm_18h_or_flip_long_at_04h():
    import json
    from pathlib import Path
    import scalp_engine as engine
    f=json.loads((Path(__file__).parent/'tests/fixtures/btc_stale_capture_20261022.json').read_text())
    state={};alerts=[]
    for cutoff in [f['baseline']]+f['cutoffs']:
        tf={k:[c for c in rows if c['t']+d<=cutoff][-n:]
            for k,d,n in [('M15',M15,999),('M5',M5,1999)]
            for rows in [f['candles'][k]]}
        state,new=chains.advance(state,'BTCUSD',[f['capture']],tf,cutoff,engine,observed_ts=cutoff+1)
        alerts.extend(new)
    breaks=[a for a in alerts if a['stage']=='M15_QUEBRA']
    assert breaks==[]  # LOW acceptance is context, never a SHORT entry.
    assert not any(a['stage']=='M5_ARMADO' and a['break_ts'] in f['cutoffs'][2:] for a in alerts)


def test_live_provider_identifies_the_verified_causal_policy(monkeypatch):
    import kairos_ludo_entry as provider
    import kairos_daily_delivery as daily
    class MapEngine:
        _kairos_build_mtf_map=staticmethod(lambda tf:{})
        _kairos_context_bias=staticmethod(lambda tf:{})
        _kairos_select_structural_first_capture_sweep=staticmethod(lambda *a:(None,{'candidates':[]}))
        _kairos_capture_radar_events=staticmethod(lambda *a:[])
    with patch.object(chains,'scan',return_value=dict(asof_ts=M15,view=chains.state_view({}))), \
         patch.object(daily,'scan_report',return_value=None):
        result=provider.snapshot('SOLUSD',dict(M15=bars(M15,M15),M5=bars(M15,M5)),M15,MapEngine)
    assert result['causal_policy_version']==chains.CAUSAL_POLICY_VERSION
