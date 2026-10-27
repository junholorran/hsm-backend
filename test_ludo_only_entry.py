import copy
from unittest.mock import patch

import pytest
import scalp_engine as engine
import kairos_capture_chains as chains
import test_live_snapshot as live_tests
from test_capture_chains import Engine, bars, capture, event, M15, M5, advance


def test_ludo_route_never_evaluates_lux_entry_or_cached_phase(monkeypatch):
    monkeypatch.setenv('KAIROS_ENTRY_STRUCTURE', 'LUDO')
    legacy = dict(old=dict(armed=True, signal_snapshot=dict(entry_observation_confirmed=True)))
    original = copy.deepcopy(legacy)
    ns, cutoff = live_tests.LiveSnapshotTests().ns(lambda *a, **kw: pytest.fail('Legacy entry evaluator called'))
    import kairos_ludo_entry as provider
    expected = dict(entry_structure_model='LUDO', sinais_unicos_completos=[], entry_phase_events=[])
    with patch.object(provider, 'snapshot', return_value=expected) as call:
        result = ns['_kairos_scan_latest_closed']('SOLUSD', cutoff, legacy)
    assert result['entry_structure_model'] == 'LUDO'
    assert result['sinais_unicos_completos'] == []
    assert legacy == original
    call.assert_called_once()


def test_provider_failure_cannot_fall_back_to_lux(monkeypatch):
    monkeypatch.setenv('KAIROS_ENTRY_STRUCTURE', 'LUDO')
    ns, cutoff = live_tests.LiveSnapshotTests().ns(lambda *a, **kw: pytest.fail('Lux fallback'))
    import kairos_ludo_entry as provider
    with patch.object(provider, 'snapshot', side_effect=RuntimeError('database unavailable')):
        result = ns['_kairos_scan_latest_closed']('SOLUSD', cutoff, {})
    assert result['erro'] == 'LUDO_ENTRY_PROVIDER_UNAVAILABLE'
    assert result['sinais_unicos_completos'] == []


def test_fvg_touch_origin_is_distinct_from_liquidity_capture():
    origin = dict(origin_kind='HTF_FVG_TOUCH', origin_key='H4|mother',
                  liquidity_tf=None, liquidity_type=None, liquidity_side=None,
                  htf_location=dict(tf='H4', zone_id='mother', tipo='FVG_bearish', bottom=103, top=109),
                  first_capture_ts=M15, native_capture_confirm_ts=2*M15)
    state, _ = advance({}, [], M15)
    state, alerts = advance(state, [origin], 2*M15)
    assert alerts == []  # A POI touch is map context, not a liquidity origin.
    state, alerts = advance(state, [origin], 3*M15, [event()])
    assert not any(a['stage']=='M5_ARMADO' for a in alerts)
    assert all(a['capture'].get('liquidity_type') is None for a in alerts)


def test_capture_presentation_uses_only_ludo_entry_gate(monkeypatch):
    monkeypatch.setenv('KAIROS_ENTRY_STRUCTURE', 'LUDO')
    from kairos_telegram_ui import format_capture_message
    msg = format_capture_message('ETHUSD', dict(liquidity_tf='H1', liquidity_type='SWING_LOW',
        level=2405.79, reaction='REJECTION_RECLAIM', timestamp=1792738800000))
    assert 'Ludo' in msg and 'Major + Internal' not in msg
    assert 'Portugal' in msg
    assert 'confirmação M15 / construção M5' not in msg


def test_ludo_only_enabled_independently_of_old_observer_flag(tmp_path, monkeypatch):
    monkeypatch.setenv('KAIROS_ENTRY_STRUCTURE', 'LUDO')
    monkeypatch.setenv('KAIROS_CAPTURE_CHAINS_ENABLED', 'false')
    path = str(tmp_path/'live.db')
    result = chains.scan('SOLUSD', {'M15': bars(M15, M15), 'M5': bars(M15, M5)},
                         M15, {}, Engine, path)
    assert result['asof_ts'] == M15
    assert result['view']['model'] == 'LUDO'


def test_chain_view_retest_is_separate_from_actual_fill():
    state, _ = advance({}, [], M15)
    state, _ = advance(state, [capture()], 2*M15)
    state, _ = advance(state, [capture()], 3*M15, [event()])
    view = chains.state_view(state)
    assert view['code'] == 'AGUARDANDO_RETESTE_M5'
    assert view['model'] == 'LUDO'
    assert view['entry_authorized'] is False
    assert view['direction'] == 'SHORT'


def test_daily_failure_does_not_block_durable_ludo_delivery(monkeypatch):
    import kairos_ludo_entry as provider
    import kairos_daily_delivery as daily
    class MapEngine:
        _kairos_build_mtf_map = staticmethod(lambda tf: {})
        _kairos_context_bias = staticmethod(lambda tf: {})
        _kairos_select_structural_first_capture_sweep = staticmethod(lambda *a: (None, {'candidates': []}))
        _kairos_capture_radar_events = staticmethod(lambda *a: [])
    with patch.object(chains, 'scan', return_value=dict(asof_ts=M15, view=chains.state_view({}))), \
         patch.object(daily, 'scan_report', side_effect=RuntimeError('presentation failure')):
        result = provider.snapshot('SOLUSD', dict(M15=bars(M15,M15), M5=bars(M15,M5)), M15, MapEngine)
    assert result['entry_structure_model'] == 'LUDO'
    assert result['sinais_unicos_completos'] == []


def test_dead_htf_origin_is_retired_before_any_entry_plan():
    origin = dict(origin_kind='HTF_FVG_TOUCH', origin_key='H1|mother',
        htf_location=dict(tf='H1', zone_id='mother', tipo='FVG_bearish', bottom=103, top=109),
        origin_zone=dict(id='mother', tipo='FVG_bearish', direcao='baixa', bottom=103,top=109),
        first_capture_ts=4*M15, sweep_ts=4*M15, native_capture_confirm_ts=5*M15)
    state, _ = advance({}, [], 3*M15)
    with patch.object(chains, '_removed_at', side_effect=lambda z,*a: 8*M15 if z and z.get('id')=='mother' else None):
        state, _ = advance(state, [origin], 5*M15)
        state, alerts = advance(state, [origin], 9*M15, [event(8*M15)])
    assert not any(a['stage'] == 'M5_ARMADO' for a in alerts)
    assert chains.state_view(state)['code'] == 'AGUARDANDO_ZONA_OU_CAPTURA'


def test_old_retest_is_not_advertised_as_current_entry_stage():
    state, _ = advance({}, [], M15)
    state, _ = advance(state, [capture()], 2*M15)
    state, _ = advance(state, [capture()], 3*M15, [event()])
    rec = next(iter(state['captures'].values()))
    rec['plan'].update(touched=True, retest=dict(t=3*M15, c=104))
    state['asof_ts'] = 5*M15
    assert chains.state_view(state)['code'] == 'RETESTE_HISTORICO_SEM_NOVA_ENTRADA'


def test_provider_accepts_no_unconfirmed_state_and_reports_ludo_model(monkeypatch):
    from kairos_context_map import enrich_report
    from test_unified_context_map import UnifiedContextMapTests
    report, tf_map, decision = UnifiedContextMapTests().inputs()
    view = dict(model='LUDO', code='AGUARDANDO_CONFIRMACAO_M15',
                setup=None, direction=None, entry_authorized=False)
    decision.update(entry_structure_model='LUDO', ludo_entry_stage=view,
                    entry_setup=dict(ready_ts=0,key='OLD_LUX',tf='M5',direction='LONG'))
    result = enrich_report(report, tf_map, decision)
    assert result['stage']['model'] == 'LUDO'
    assert result['stage']['setup'] is None


def test_radar_failure_does_not_block_durable_ludo_delivery():
    import kairos_ludo_entry as provider
    class MapEngine:
        _kairos_build_mtf_map = staticmethod(lambda tf: {})
        _kairos_context_bias = staticmethod(lambda tf: {})
        _kairos_select_structural_first_capture_sweep = staticmethod(lambda *a: (None, {'candidates': []}))
        @staticmethod
        def _kairos_capture_radar_events(*args):
            raise RuntimeError('radar rendering failed')
    with patch.object(chains, 'scan', return_value=dict(asof_ts=M15, view=chains.state_view({}))):
        result = provider.snapshot('SOLUSD', dict(M15=bars(M15,M15), M5=bars(M15,M5)), M15, MapEngine)
    assert result['entry_structure_model'] == 'LUDO'
    assert result['radar_captures'] == []


def test_new_fvg_touch_in_first_interval_after_activation_survives_restart(tmp_path, monkeypatch):
    monkeypatch.setenv('KAIROS_ENTRY_STRUCTURE', 'LUDO')
    path = str(tmp_path/'state.db')
    origin = dict(origin_kind='HTF_FVG_TOUCH', origin_key='H1|mother',
        htf_location=dict(tf='H1', zone_id='mother', tipo='FVG_bearish', bottom=103, top=109),
        first_capture_ts=M15, native_capture_confirm_ts=2*M15)
    chains.scan('SOLUSD', {'M15':bars(M15,M15),'M5':bars(M15,M5)}, M15, {}, Engine, path)
    decision = dict(htf_entry_origins=[origin])
    with patch.object(chains,'structure_snapshot',return_value=dict(status='OK',events=[])):
        saved = chains.scan('SOLUSD', {'M15':bars(2*M15,M15),'M5':bars(2*M15,M5)},2*M15,decision,Engine,path)
    assert saved['view']['code'] == 'AGUARDANDO_ZONA_OU_CAPTURA'
    with patch.object(chains,'structure_snapshot',return_value=dict(status='OK',events=[event()])):
        saved = chains.scan('SOLUSD', {'M15':bars(3*M15,M15),'M5':bars(3*M15,M5)},3*M15,decision,Engine,path)
    assert saved['view']['code'] == 'AGUARDANDO_ZONA_OU_CAPTURA'
    assert saved['view']['setup'] is None


def test_invalid_live_structure_configuration_fails_closed(monkeypatch):
    monkeypatch.setenv('KAIROS_ENTRY_STRUCTURE','LUDOO')
    import kairos_ludo_entry as provider
    with pytest.raises(ValueError):
        provider.enabled()
