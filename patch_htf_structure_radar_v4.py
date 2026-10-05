from pathlib import Path
p=Path('scalp_engine.py')
s=p.read_text(encoding='utf-8')
MARK='# KAIROS_HTF_STRUCTURE_RADAR_V5_ARMED'
if MARK not in s:
    a="def _paper_trading_v2_enviar_telegram(mensagem):\n"
    if a not in s: raise SystemExit('telegram anchor missing')
    b=r'''# KAIROS_HTF_STRUCTURE_RADAR_V5_ARMED
# State machine: relevant W1/D1/H4/H1 sweep ARMS the radar.
# The armed capture persists until the first CLOSED M15 or M5 CHoCH.
# No second M5 sweep, FVG, OB, retest, SL or RR is required for this structural alert.
def _kairos_first_choch_after_armed_sweep(candles, sweep_ts, swing_size, tf):
    found=[]
    for ev in compute_lux_structure_events(candles, swing_size=swing_size):
        if ev.get('tipo') != 'CHoCH':
            continue
        open_ts=ev.get('t')
        if open_ts is None:
            continue
        close_ts=_kairos_candle_close_ts(open_ts, tf)
        if close_ts is not None and close_ts > sweep_ts:
            found.append((close_ts, ev))
    if not found:
        return None
    close_ts,ev=min(found,key=lambda z:z[0])
    return {
        'tf':tf,
        'direction':'LONG' if ev.get('direcao')=='alta' else 'SHORT',
        'type':'CHoCH',
        'level':ev.get('nivel'),
        'open_ts':ev.get('t'),
        'close_ts':close_ts,
    }

'''
    s=s.replace(a,b+a,1)

    anchor="        try:\n            r = avaliar_vortex_decision_layer_v2(\n"
    if anchor not in s: raise SystemExit('causal replay anchor missing')
    runtime="""        # KAIROS V5 structural radar state machine. Locals persist across this replay loop.\n        if '_kairos_v5_armed' not in locals():\n            _kairos_v5_armed=None\n            _kairos_v5_consumed=set()\n\n        # Only look for a new relevant HTF capture while the radar is not already armed.\n        if _kairos_v5_armed is None:\n            try:\n                _cap,_cap_audit=_kairos_select_structural_first_capture_sweep(tf_map,ts_corte,liquidity_policy=liquidity_policy)\n                if _cap and _cap.get('liquidity_tf') in ('W1','D1','H4','H1'):\n                    _cap_key=(_cap.get('liquidity_tf'),_cap.get('liquidity_type'),_cap.get('liquidity_level'),_cap.get('sweep_ts'))\n                    if _cap_key not in _kairos_v5_consumed:\n                        _kairos_v5_armed=dict(_cap)\n                        _kairos_v5_armed['_radar_key']=_cap_key\n                        print(f\"[KAIROS_SWEEP_ARMED] pair={pair} htf={_cap.get('liquidity_tf')} type={_cap.get('liquidity_type')} level={_cap.get('liquidity_level')} sweep_ts={_cap.get('sweep_ts')}\",flush=True)\n            except Exception as _e:\n                print(f'[KAIROS_SWEEP_ARM_ERROR] pair={pair} error={_e}',flush=True)\n\n        # Once armed, DO NOT re-select or revalidate the sweep. Wait for CHoCH only.\n        if _kairos_v5_armed is not None:\n            try:\n                _sweep_ts=_kairos_v5_armed.get('sweep_ts')\n                _m15=_kairos_first_choch_after_armed_sweep(tf_map.get('M15') or [],_sweep_ts,50,'15')\n                _m5=_kairos_first_choch_after_armed_sweep(tf_map.get('M5') or [],_sweep_ts,5,'5')\n                _candidates=[x for x in (_m15,_m5) if x]\n                _choch=min(_candidates,key=lambda x:x['close_ts']) if _candidates else None\n                if _choch:\n                    _cap=_kairos_v5_armed\n                    print(f\"[KAIROS_STRUCTURAL_RADAR_SIGNAL] pair={pair} direction={_choch.get('direction')} htf={_cap.get('liquidity_tf')} liquidity_type={_cap.get('liquidity_type')} liquidity_level={_cap.get('liquidity_level')} sweep_ts={_cap.get('sweep_ts')} structure_tf={_choch.get('tf')} structure_type=CHoCH structure_level={_choch.get('level')} structure_close_ts={_choch.get('close_ts')}\",flush=True)\n                    _kairos_v5_consumed.add(_cap.get('_radar_key'))\n                    _kairos_v5_armed=None\n            except Exception as _e:\n                print(f'[KAIROS_CHoCH_WATCH_ERROR] pair={pair} error={_e}',flush=True)\n\n        try:\n            r = avaliar_vortex_decision_layer_v2(\n"""
    s=s.replace(anchor,runtime,1)

p.write_text(s,encoding='utf-8')
compile(s,'scalp_engine.py','exec')
assert MARK in s
assert '[KAIROS_SWEEP_ARMED]' in s
assert '[KAIROS_STRUCTURAL_RADAR_SIGNAL]' in s
assert "ev.get('tipo') != 'CHoCH'" in s
assert "('W1','D1','H4','H1')" in s
print('[KAIROS_HTF_STRUCTURE_V5] ARMED -> CHoCH compile PASS')
