from patch_kairos_htf_fvg_alerts import patch_scalp_source, patch_app_source


def test_scalp_patch_exports_fvg_events_without_extra_fetch_or_signal_gate():
    source = """def _kairos_scan_latest_closed(pair, observed_ts, phase_state):\n    tf_map={}\n    signals=[value['signal_snapshot'] for value in phase_state.values() if value.get('signal_snapshot')]\n    return {\n            'radar_captures':_kairos_capture_radar_events(r,tf_map,cutoff),\n            'sinais_unicos_completos':signals}\n"""
    patched = patch_scalp_source(source)
    assert "_kairos_fvg_states(tf_map.get(_tf) or [])" in patched
    assert "_fetch_bybit" not in patched
    assert "'htf_fvg_touch_events':htf_fvg_touch_events" in patched


def test_app_patch_adds_separate_fvg_delivery_without_replacing_liquidity():
    source = """_KAIROS_LIVE_PAIRS=('BTCUSD',)\n_KAIROS_SETUP_SEEN={pair:set() for pair in _KAIROS_LIVE_PAIRS}\ndef loop():\n                    delivered = _kairos_send_capture_events(pair, r, cycle_started_ms)\n                    delivered = _kairos_send_setup_events(pair, r, cycle_started_ms) and delivered\n"""
    patched = patch_app_source(source)
    assert '_kairos_send_capture_events' in patched
    assert '_kairos_send_fvg_events' in patched
    assert 'kairos_fvg_delivery' in patched
    assert 'format_fvg_touch_message' in patched
