from pathlib import Path
p=Path('scalp_engine.py')
s=p.read_text(encoding='utf-8')
MARK='# KAIROS_HTF_STRUCTURE_RADAR_V6_ENGINE_CAPTURE'
if MARK not in s:
    a="def _paper_trading_v2_enviar_telegram(mensagem):\n"
    if a not in s: raise SystemExit('telegram anchor missing')
    b=r'''# KAIROS_HTF_STRUCTURE_RADAR_V6_ENGINE_CAPTURE
# Relevant W1/D1/H4/H1 sweep -> persistent ARMED state -> first CLOSED M15/M5 CHoCH.
def _kairos_first_choch_after_armed_sweep(candles, sweep_ts, swing_size, tf):
    found=[]
    for ev in compute_lux_structure_events(candles, swing_size=swing_size):
        if ev.get('tipo') != 'CHoCH':
            continue
        open_ts=ev.get('t')
        if open_ts is None:
            continue
        close_ts=_kairos_candle_close_ts(open_ts,tf)
        if close_ts is not None and close_ts>sweep_ts:
            found.append((close_ts,ev))
    if not found: return None
    close_ts,ev=min(found,key=lambda z:z[0])
    return {'tf':tf,'direction':'LONG' if ev.get('direcao')=='alta' else 'SHORT','type':'CHoCH','level':ev.get('nivel'),'open_ts':ev.get('t'),'close_ts':close_ts}

'''
    s=s.replace(a,b+a,1)

    # Initialize replay-local persistent radar state once, before the causal M5 loop.
    loop_anchor="    for i in range(MIN_M5_IDX, len(m5)):\n"
    if loop_anchor not in s: raise SystemExit('replay loop anchor missing')
    init="""    _kairos_v6_armed=None\n    _kairos_v6_consumed=set()\n    _kairos_v6_signals=set()\n\n    for i in range(MIN_M5_IDX, len(m5)):\n"""
    s=s.replace(loop_anchor,init,1)

    # The full engine already proves/returns the structural capture. Arm from THAT exact capture,
    # so the radar cannot disagree with a capture the engine itself recognized.
    post_anchor="""            r = avaliar_vortex_decision_layer_v2(\n                m15_ate_agora, m5_ate_agora, d1_ate_agora, candles_por_tf=tf_map,\n                audit_pair=pair,\n                experimental_poi_policy=experimental_poi_policy,\n                experimental_poi_state=experimental_poi_state,\n                liquidity_policy=liquidity_policy\n            )\n"""
    if post_anchor not in s: raise SystemExit('engine call anchor missing')
    post=post_anchor+"""
            # ARM from the exact HTF capture already accepted by the engine, before any downstream
            # FVG/OB/retest/SL/RR result can matter to this structural radar.
            if _kairos_v6_armed is None and r.get('first_capture_ts') is not None and r.get('liquidity_tf') in ('W1','D1','H4','H1'):
                _cap_key=(r.get('liquidity_tf'),r.get('liquidity_type'),r.get('sweep_level'),r.get('first_capture_ts'))
                if _cap_key not in _kairos_v6_consumed:
                    _kairos_v6_armed={'liquidity_tf':r.get('liquidity_tf'),'liquidity_type':r.get('liquidity_type'),'nivel':r.get('sweep_level'),'sweep_ts':r.get('first_capture_ts'),'extremo':r.get('sweep_extreme'),'_radar_key':_cap_key}
                    print(f\"[KAIROS_SWEEP_ARMED] pair={pair} htf={r.get('liquidity_tf')} type={r.get('liquidity_type')} level={r.get('sweep_level')} sweep_ts={r.get('first_capture_ts')} extreme={r.get('sweep_extreme')}\",flush=True)

            # Once armed, never re-select/revalidate the sweep. Only a closed CHoCH can consume it.
            if _kairos_v6_armed is not None:
                _st=_kairos_v6_armed.get('sweep_ts')
                _m15=_kairos_first_choch_after_armed_sweep(tf_map.get('M15') or [],_st,50,'15')
                _m5=_kairos_first_choch_after_armed_sweep(tf_map.get('M5') or [],_st,5,'5')
                _cand=[x for x in (_m15,_m5) if x and x.get('close_ts')<=ts_corte]
                _choch=min(_cand,key=lambda x:x['close_ts']) if _cand else None
                if _choch:
                    _cap=_kairos_v6_armed
                    _sig=(_cap.get('_radar_key'),_choch.get('tf'),_choch.get('close_ts'),_choch.get('direction'))
                    if _sig not in _kairos_v6_signals:
                        _kairos_v6_signals.add(_sig)
                        print(f\"[KAIROS_STRUCTURAL_RADAR_SIGNAL] pair={pair} direction={_choch.get('direction')} htf={_cap.get('liquidity_tf')} liquidity_type={_cap.get('liquidity_type')} liquidity_level={_cap.get('nivel')} sweep_ts={_cap.get('sweep_ts')} structure_tf={_choch.get('tf')} structure_type=CHoCH structure_level={_choch.get('level')} structure_close_ts={_choch.get('close_ts')}\",flush=True)
                    _kairos_v6_consumed.add(_cap.get('_radar_key'))
                    _kairos_v6_armed=None
"""
    s=s.replace(post_anchor,post,1)

p.write_text(s,encoding='utf-8')
compile(s,'scalp_engine.py','exec')
assert MARK in s
assert '[KAIROS_SWEEP_ARMED]' in s
assert '[KAIROS_STRUCTURAL_RADAR_SIGNAL]' in s
assert "r.get('first_capture_ts')" in s
assert "('W1','D1','H4','H1')" in s
print('[KAIROS_HTF_STRUCTURE_V6] ENGINE_CAPTURE -> ARMED -> CHoCH compile PASS')
