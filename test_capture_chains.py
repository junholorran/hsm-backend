import copy
import json
import sqlite3
from unittest.mock import patch

import kairos_capture_chains as chains

M5 = 300000
M15 = 900000


def bars(cutoff, duration, price=100):
    return [dict(t=t, o=price, h=price+1, l=price-1, c=price)
            for t in range(0, cutoff, duration) if t+duration <= cutoff]


def capture(opened=M15, level=108):
    return dict(liquidity_tf='H4', liquidity_type='SWING_HIGH', nivel=level,
                liquidity_origin_ts=0, first_capture_ts=opened,
                native_capture_confirm_ts=opened+M15, liquidity_side='HIGH')


def event(opened=2*M15, side='DOWN'):
    return dict(kind='CHoCH', side=side, level=102, origin_ts=M15,
                level_known_ts=2*M15, open_ts=opened, close_ts=opened+M15,
                index=opened//M15, opposite_level=110, opposite_origin_ts=M15)


class Engine:
    @staticmethod
    def _kairos_select_entry_zone(cs, sweep, structure, mapa):
        return dict(id='parent', tipo='FVG_bearish', direcao=sweep['direcao'], top=106,
                    bottom=104, created_ts=structure['t'])

    @staticmethod
    def _kairos_m5_refine_zone(*args, **kwargs):
        return dict(id='child', tipo='FVG_bearish', lux_fvg=True, direcao=args[4], top=105, bottom=104,
                    source_a={'t': M15}, source_mid={'t': M15+M5}, source_c={'t': M15+2*M5})

    @staticmethod
    def _kairos_fvg_states(cs, lookback=250):
        return [dict(id='parent', state='ATIVA'), dict(id='child', state='ATIVA')]


def advance(state, captures, cutoff, events=(), m5=None):
    tf = {'M15': bars(cutoff, M15), 'M5': m5 or bars(cutoff, M5)}
    snap = dict(status='OK', events=list(events))
    with patch.object(chains, 'structure_snapshot', return_value=snap):
        return chains.advance(state, 'SOLUSD', captures, tf, cutoff, Engine)


def test_bootstrap_does_not_notify_old_capture_or_historical_retest():
    state, alerts = advance({}, [capture(0)], 2*M15)
    assert alerts == []
    state, alerts = advance(state, [capture(0)], 3*M15, [event()])
    assert alerts == []
    assert not state['captures']


def test_second_capture_does_not_reuse_old_break_after_first_invalidation():
    state, _ = advance({}, [], M15)
    state, alerts = advance(state, [capture()], 2*M15)
    assert [a['stage'] for a in alerts] == ['CAPTURA']
    state, alerts = advance(state, [capture()], 3*M15, [event()])
    assert 'M5_ARMADO' in [a['stage'] for a in alerts]
    c2 = capture(3*M15, 112)
    state, alerts = advance(state, [capture(), c2], 4*M15, [event()])
    assert len(state['captures']) == 2
    second = state['captures'][chains.capture_id('SOLUSD', c2)]
    assert second.get('plan') is None
    newer = event(4*M15)
    state, alerts = advance(state, [c2], 5*M15, [event(), newer])
    assert second['capture']['nivel'] == 112
    assert any(a['capture_id'] == chains.capture_id('SOLUSD', c2)
               and a['stage'] == 'M15_QUEBRA' for a in alerts)


def test_m15_before_m5_both_directions_and_same_cutoff_deduplication():
    state, _ = advance({}, [], M15)
    state, _ = advance(state, [capture()], 2*M15)
    state, alerts = advance(state, [capture()], 3*M15, [event()])
    assert [a['stage'] for a in alerts] == ['M15_QUEBRA', 'M5_ARMADO']
    frozen = copy.deepcopy(state)
    same, alerts = advance(state, [capture()], 3*M15, [event()])
    assert same == frozen and alerts == []
    state, alerts = advance(state, [capture()], 4*M15, [event(), event(3*M15, 'UP')])
    assert [a['stage'] for a in alerts] == ['PLANO_INVALIDADO']
    # Opposite structure alone cannot recycle a consumed, unchanged reaction.
    assert not any(a['stage'] == 'M5_ARMADO' for a in alerts)


def test_retest_is_prospective_and_never_assumes_fill():
    state, _ = advance({}, [], M15)
    state, _ = advance(state, [capture()], 2*M15)
    # The break arrives late: old touches cannot be called prospective.
    rows = bars(4*M15, M5, 104.5)
    state, alerts = advance(state, [capture()], 4*M15, [event()], rows)
    assert all(a['stage'] != 'RETESTE_OBSERVADO' for a in alerts)
    rows += [dict(t=4*M15, o=103, h=104.5, l=103, c=104)]
    state, alerts = advance(state, [capture()], 4*M15+M5, [event()], rows)
    assert alerts[-1]['stage'] == 'RETESTE_OBSERVADO'
    assert alerts[-1]['fill_assumed'] is False


def test_touch_and_invalidation_same_candle_is_unresolved():
    state, _ = advance({}, [], M15)
    state, _ = advance(state, [capture()], 2*M15)
    state, _ = advance(state, [capture()], 3*M15, [event()])
    rows = bars(3*M15, M5) + [dict(t=3*M15, o=103, h=112, l=102, c=111)]
    _, alerts = advance(state, [capture()], 3*M15+M5, [event()], rows)
    assert alerts[-1]['stage'] == 'TOQUE_E_INVALIDACAO'
    assert alerts[-1]['fill_assumed'] is False


def test_gap_does_not_advance_watermark_and_forming_break_is_ignored():
    state, _ = advance({}, [], M15)
    gap = bars(2*M15, M5)
    gap.pop(1)
    same, alerts = advance(state, [capture()], 2*M15, m5=gap)
    assert same == state and alerts == []
    state, _ = advance(state, [capture()], 2*M15)
    _, alerts = advance(state, [capture()], 2*M15+M5, [event()])
    assert alerts == []


def test_storage_restart_and_failed_sender_retries_same_pending_message(tmp_path, monkeypatch):
    monkeypatch.setenv('KAIROS_CAPTURE_CHAINS_ENABLED', 'true')
    path = str(tmp_path/'events.db')
    tf = {'M15': bars(M15, M15), 'M5': bars(M15, M5)}
    chains.scan('SOLUSD', tf, M15, {'structural_sweep_audit': {'candidates': []}}, Engine, path)
    tf = {'M15': bars(2*M15, M15), 'M5': bars(2*M15, M5)}
    r = {'structural_sweep_audit': {'candidates': [capture()]}}
    chains.scan('SOLUSD', tf, 2*M15, r, Engine, path)
    messages = []
    assert chains.deliver('SOLUSD', lambda s: messages.append(s) or False, path) is False
    assert chains.deliver('SOLUSD', lambda s: messages.append(s) or True, path) is True
    assert messages[0] == messages[1]
    chains.scan('SOLUSD', tf, 2*M15, r, Engine, path)
    assert chains.deliver('SOLUSD', lambda s: messages.append(s) or True, path) is True
    assert len(messages) == 2
    with sqlite3.connect(path) as db:
        assert db.execute('select count(*) from chain_outbox').fetchone()[0] == 1


def test_disabled_does_not_create_storage(tmp_path, monkeypatch):
    monkeypatch.delenv('KAIROS_CAPTURE_CHAINS_ENABLED', raising=False)
    path = str(tmp_path/'unused.db')
    assert chains.scan('SOLUSD', {}, 0, {}, Engine, path) is None
    assert not (tmp_path/'unused.db').exists()


def test_opposite_m15_break_precedes_later_m5_touch_during_catchup():
    state, _ = advance({}, [], M15)
    state, _ = advance(state, [capture()], 2*M15)
    state, _ = advance(state, [capture()], 3*M15, [event()])
    rows = bars(4*M15+M5, M5)
    rows[-1] = dict(t=4*M15, o=103, h=104.5, l=103, c=104)
    _, alerts = advance(state, [capture()], 4*M15+M5, [event(), event(3*M15, 'UP')], rows)
    assert not any(a['stage'] == 'RETESTE_OBSERVADO' for a in alerts)


def test_late_break_cannot_arm_after_protected_extreme_was_breached():
    state, _ = advance({}, [], M15)
    state, _ = advance(state, [capture()], 2*M15)
    class HighZone(Engine):
        @staticmethod
        def _kairos_m5_refine_zone(*args, **kw):
            return dict(tipo='OB_bearish', direcao='baixa', top=121, bottom=120,
                        break_ts=2*M15, t=M15)
    tf = {'M15': bars(4*M15, M15), 'M5': bars(4*M15, M5, 111)}
    with patch.object(chains, 'structure_snapshot', return_value=dict(status='OK', events=[event()])):
        _, alerts = chains.advance(state, 'SOLUSD', [capture()], tf, 4*M15, HighZone)
    assert not any(a['stage'] == 'M5_ARMADO' for a in alerts)


def test_native_fvg_wick_removal_is_not_replaced_with_close_rule():
    import scalp_engine as engine
    cs = [dict(t=i*M5, o=100, h=101, l=99, c=100) for i in range(8)]
    cs[-2] = dict(t=6*M5, o=100, h=111, l=100, c=110)
    cs[-1] = dict(t=7*M5, o=110, h=112, l=109, c=111)
    zone = next(z for z in engine._kairos_fvg_states(cs) if z.get('lux_fvg'))
    removal = dict(t=8*M5, o=110, h=111, l=100, c=110)
    assert removal['c'] > zone['bottom'] and removal['l'] < zone['bottom']
    assert chains._removed_at(zone, cs+[removal], M5, engine) == 9*M5


def test_removed_initial_zone_falls_back_to_later_m5_zone_at_its_own_birth():
    class Zones(Engine):
        @staticmethod
        def _kairos_m5_refine_zone(*args, **kw):
            return dict(id='old', tipo='FVG_bearish', lux_fvg=True, top=106, bottom=104,
                        direcao='baixa', created_ts=2*M15,
                        source_a={'t': M15}, source_mid={'t': M15+M5}, source_c={'t': M15+2*M5})
        @staticmethod
        def _kairos_fvg_states(cs, lookback=250):
            return [dict(id='old', state='INVALIDADA', invalidated_ts=3*M15),
                    dict(id='new', state='ATIVA')]
        @staticmethod
        def _kairos_select_entry_zone(*args):
            return dict(id='new', tipo='FVG_bearish', lux_fvg=True, top=106, bottom=104,
                        direcao='baixa', created_ts=4*M15,
                        source_a={'t': 4*M15-2*M5}, source_mid={'t': 4*M15-M5}, source_c={'t': 4*M15})
    m5event = event(4*M15)
    m5event['close_ts'] = 4*M15+M5
    zone, ready = chains._refinement(Zones, bars(5*M15, M5), capture(), event(),
                                    dict(top=108, bottom=103), [m5event])
    assert zone['id'] == 'new' and ready == 4*M15+M5


def test_dead_parent_cannot_arm_refinement():
    class DeadParent(Engine):
        @staticmethod
        def _kairos_fvg_states(cs, lookback=250):
            return [dict(id='parent', state='INVALIDADA', invalidated_ts=3*M15)]
    state, _ = advance({}, [], M15)
    state, _ = advance(state, [capture()], 2*M15)
    tf = {'M15': bars(4*M15, M15), 'M5': bars(4*M15, M5)}
    with patch.object(chains, 'structure_snapshot', return_value=dict(status='OK', events=[event()])):
        _, alerts = chains.advance(state, 'SOLUSD', [capture()], tf, 4*M15, DeadParent)
    assert all(a['stage'] != 'M5_ARMADO' for a in alerts)


def test_actual_observation_time_excludes_already_open_m5_retest():
    state, _ = advance({}, [], M15)
    state, _ = advance(state, [capture()], 2*M15)
    tf = {'M15': bars(3*M15, M15), 'M5': bars(3*M15, M5)}
    with patch.object(chains, 'structure_snapshot', return_value=dict(status='OK', events=[event()])):
        state, _ = chains.advance(state, 'SOLUSD', [capture()], tf, 3*M15, Engine,
                                  observed_ts=3*M15+20000)
    rows = bars(3*M15, M5)+[dict(t=3*M15, o=103, h=104.5, l=103, c=104)]
    _, alerts = advance(state, [capture()], 3*M15+M5, [event()], rows)
    assert all(a['stage'] != 'RETESTE_OBSERVADO' for a in alerts)


def test_new_refinement_is_not_armed_in_the_wrong_direction():
    class WrongWay(Engine):
        @staticmethod
        def _kairos_m5_refine_zone(*args, **kw):
            return dict(id='child', tipo='FVG_bullish', direcao='alta', top=105, bottom=104)
    zone, _ = chains._refinement(WrongWay, bars(3*M15, M5), capture(), event(),
                                dict(top=106, bottom=104), [])
    assert zone is None


def test_m5_fvg_cannot_borrow_source_candles_from_before_capture():
    class OldMother(Engine):
        @staticmethod
        def _kairos_m5_refine_zone(*args, **kw):
            return dict(id='child', tipo='FVG_bearish', direcao='baixa', top=105, bottom=104,
                        source_a={'t': 0}, source_mid={'t': M5}, source_c={'t': 2*M5})
    zone, _ = chains._refinement(OldMother, bars(3*M15, M5), capture(), event(),
                                dict(top=106, bottom=104), [])
    assert zone is None


def test_observer_failure_does_not_mutate_legacy_decision_or_phase_state():
    from test_live_snapshot import LiveSnapshotTests
    result = dict(valid=False, failure_reason='UNCHANGED')
    ns, cutoff = LiveSnapshotTests().ns(lambda *a, **kw: result)
    with patch.object(chains, 'scan', side_effect=RuntimeError('storage unavailable')):
        output = ns['_kairos_scan_latest_closed']('SOLUSD', cutoff, {})
    assert result == dict(valid=False, failure_reason='UNCHANGED')
    assert output['sinais_unicos_completos'] == []


def test_native_sol_m15_break_and_prospective_m5_retest():
    from datetime import datetime, timezone
    from pathlib import Path
    import scalp_engine as engine
    def ts(hour, minute=0):
        return int(datetime(2026, 10, 21, hour, minute, tzinfo=timezone.utc).timestamp()*1000)
    tf = json.loads((Path(__file__).parent/'tests/fixtures/sol_chain_20261021.json').read_text())['candles']
    cap = dict(liquidity_tf='D1', liquidity_type='PDH', nivel=108.22,
               liquidity_origin_ts=ts(0)-86400000, liquidity_side='HIGH',
               first_capture_ts=ts(15), sweep_ts=ts(15), native_capture_confirm_ts=ts(15,15))
    state = {}; alerts = []
    for cutoff in range(ts(14,45), ts(21)+1, M5):
        state, new = chains.advance(state, 'SOLUSD', [cap], tf, cutoff, engine)
        alerts.extend(new)
    bearish = [a for a in alerts if a['stage'] == 'M15_QUEBRA' and a['direction'] == 'SHORT']
    assert bearish[0]['break_ts'] == ts(19,15)  # 20:15 Portugal, body break 107.87.
    assert bearish[0]['level'] == 107.87
    armed = next(a for a in alerts if a['stage'] == 'M5_ARMADO' and a['direction'] == 'SHORT')
    retest = next(a for a in alerts if a['stage'] == 'RETESTE_OBSERVADO' and a['direction'] == 'SHORT')
    assert armed['observed_ts'] <= retest['candle_ts']
    assert retest['entry_authorized'] is False and retest['fill_assumed'] is False
