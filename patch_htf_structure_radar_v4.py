from pathlib import Path
p=Path('scalp_engine.py')
s=p.read_text(encoding='utf-8')
MARK='# KAIROS_HTF_STRUCTURE_RADAR_V7_CAPTURE_POINT'
if MARK not in s:
    helper_anchor="def _paper_trading_v2_enviar_telegram(mensagem):\n"
    if helper_anchor not in s: raise SystemExit('telegram anchor missing')
    helper=r'''# KAIROS_HTF_STRUCTURE_RADAR_V7_CAPTURE_POINT
# Radar independente: HTF sweep reconhecido -> ARMED -> primeiro CHoCH fechado M15/M5.
def _kairos_radar_first_closed_choch(candles, sweep_ts, swing_size, tf):
    out=[]
    for ev in compute_lux_structure_events(candles, swing_size=swing_size):
        if ev.get('tipo') != 'CHoCH':
            continue
        ots=ev.get('t')
        if ots is None:
            continue
        cts=_kairos_candle_close_ts(ots,tf)
        if cts is not None and cts>sweep_ts:
            out.append((cts,ev))
    if not out:
        return None
    cts,ev=min(out,key=lambda x:x[0])
    return {'tf':tf,'direction':'LONG' if ev.get('direcao')=='alta' else 'SHORT','level':ev.get('nivel'),'close_ts':cts}

'''
    s=s.replace(helper_anchor,helper+helper_anchor,1)

    loop="    for i in range(MIN_M5_IDX, len(m5)):\n"
    if loop not in s: raise SystemExit('replay loop missing')
    init="""    _kairos_v7_armed=None
    _kairos_v7_consumed=set()
    _kairos_v7_signals=set()

    for i in range(MIN_M5_IDX, len(m5)):
"""
    s=s.replace(loop,init,1)

    selector="""    sweep,sweep_audit=_kairos_select_structural_first_capture_sweep(candles_por_tf,now_ts,liquidity_policy=liquidity_policy)
    resultado['structural_sweep_audit']=sweep_audit
"""
    if selector not in s: raise SystemExit('selector anchor missing')
    tapped="""    sweep,sweep_audit=_kairos_select_structural_first_capture_sweep(candles_por_tf,now_ts,liquidity_policy=liquidity_policy)
    resultado['structural_sweep_audit']=sweep_audit
    # Exporta a captura no ponto exato em que o motor a reconhece.
    if sweep:
        resultado['_radar_capture']={
            'liquidity_tf':sweep.get('liquidity_tf'),
            'liquidity_type':sweep.get('liquidity_type'),
            'nivel':sweep.get('nivel'),
            'sweep_ts':sweep.get('first_capture_ts') or sweep.get('sweep_ts'),
            'extremo':sweep.get('extremo'),
        }
"""
    s=s.replace(selector,tapped,1)

    call="""            r = avaliar_vortex_decision_layer_v2(
                m15_ate_agora, m5_ate_agora, d1_ate_agora, candles_por_tf=tf_map,
                audit_pair=pair,
                experimental_poi_policy=experimental_poi_policy,
                experimental_poi_state=experimental_poi_state,
                liquidity_policy=liquidity_policy, cutoff_ts=ts_corte
            )
"""
    if call not in s: raise SystemExit('engine call missing')
    logic=call+"""
            _cap=r.get('_radar_capture') or {}
            _cap_tf=_cap.get('liquidity_tf')
            _cap_ts=_cap.get('sweep_ts')
            if _kairos_v7_armed is None and _cap_tf in ('W1','D1','H4','H1') and _cap_ts is not None:
                _key=(_cap_tf,_cap.get('liquidity_type'),_cap.get('nivel'),_cap_ts)
                if _key not in _kairos_v7_consumed:
                    _kairos_v7_armed=dict(_cap)
                    _kairos_v7_armed['_radar_key']=_key
                    print(f\"[KAIROS_SWEEP_ARMED] pair={pair} htf={_cap_tf} type={_cap.get('liquidity_type')} level={_cap.get('nivel')} sweep_ts={_cap_ts} extreme={_cap.get('extremo')}\",flush=True)

            if _kairos_v7_armed is not None:
                _st=_kairos_v7_armed['sweep_ts']
                _m15=_kairos_radar_first_closed_choch(tf_map.get('M15') or [],_st,50,'15')
                _m5=_kairos_radar_first_closed_choch(tf_map.get('M5') or [],_st,5,'5')
                _available=[x for x in (_m15,_m5) if x and x['close_ts']<=ts_corte]
                _choch=min(_available,key=lambda x:x['close_ts']) if _available else None
                if _choch:
                    _a=_kairos_v7_armed
                    _sig=(_a['_radar_key'],_choch['tf'],_choch['close_ts'],_choch['direction'])
                    if _sig not in _kairos_v7_signals:
                        _kairos_v7_signals.add(_sig)
                        print(f\"[KAIROS_STRUCTURAL_RADAR_SIGNAL] pair={pair} direction={_choch['direction']} htf={_a.get('liquidity_tf')} liquidity_type={_a.get('liquidity_type')} liquidity_level={_a.get('nivel')} sweep_ts={_a.get('sweep_ts')} structure_tf={_choch['tf']} structure_type=CHoCH structure_level={_choch['level']} structure_close_ts={_choch['close_ts']}\",flush=True)
                    _kairos_v7_consumed.add(_a['_radar_key'])
                    _kairos_v7_armed=None
"""
    s=s.replace(call,logic,1)

p.write_text(s,encoding='utf-8')
compile(s,'scalp_engine.py','exec')
assert MARK in s
assert "resultado['_radar_capture']" in s
assert '[KAIROS_SWEEP_ARMED]' in s
assert '[KAIROS_STRUCTURAL_RADAR_SIGNAL]' in s
print('[KAIROS_HTF_STRUCTURE_V7] CAPTURE_POINT -> ARMED -> CHoCH compile PASS')
