from pathlib import Path
p=Path('scalp_engine.py')
s=p.read_text(encoding='utf-8')
MARK='# KAIROS_HTF_STRUCTURE_RADAR_V4'
if MARK not in s:
    a="def _paper_trading_v2_enviar_telegram(mensagem):\n"
    if a not in s: raise SystemExit('anchor missing')
    b=r'''# KAIROS_HTF_STRUCTURE_RADAR_V4
# Relevant HTF capture -> first confirmed M15/M5 structure break.
def _kairos_structure_confirmation_after_capture(candles, capture_ts, swing_size, tf):
    found=[]
    for ev in compute_lux_structure_events(candles, swing_size=swing_size):
        ot=ev.get('t')
        if ot is None: continue
        ct=_kairos_candle_close_ts(ot,tf)
        if ct is not None and ct>capture_ts:
            found.append((ct,ev))
    if not found: return None
    ct,ev=min(found,key=lambda z:z[0])
    return {'tf':tf,'direction':'LONG' if ev.get('direcao')=='alta' else 'SHORT','type':ev.get('tipo'),'level':ev.get('nivel'),'open_ts':ot,'close_ts':ct}

def _kairos_radar_after_htf_capture(candles_por_tf, cutoff, liquidity_policy='A_CURRENT'):
    snap={k:_kairos_candles_fechados_ate(candles_por_tf.get(k) or [],t,cutoff) for k,t in (('MN','M'),('W1','W'),('D1','D'),('H4','240'),('H1','60'),('M15','15'))}
    cap,audit=_kairos_select_structural_first_capture_sweep(snap,cutoff,liquidity_policy=liquidity_policy)
    if not cap: return {'valid':False,'reason':'NO_RELEVANT_HTF_CAPTURE'}
    if cap.get('liquidity_tf') not in ('W1','D1','H4','H1'): return {'valid':False,'reason':'CAPTURE_NOT_PRIMARY_HTF'}
    st=cap.get('sweep_ts')
    e15=_kairos_structure_confirmation_after_capture(candles_por_tf.get('M15') or [],st,50,'15')
    e5=_kairos_structure_confirmation_after_capture(candles_por_tf.get('M5') or [],st,5,'5')
    candidates=[x for x in (e15,e5) if x]
    chosen=min(candidates,key=lambda x:x['close_ts']) if candidates else None
    if not chosen: return {'valid':False,'reason':'WAIT_STRUCTURE_BREAK_AFTER_HTF_CAPTURE','capture':cap}
    return {'valid':True,'direction':chosen['direction'],'structure':chosen,'capture':cap}

'''
    s=s.replace(a,b+a,1)
    anchor="        try:\n            r = avaliar_vortex_decision_layer_v2(\n"
    if anchor not in s: raise SystemExit('replay anchor missing')
    runtime="""        if '_kairos_v4_seen' not in locals(): _kairos_v4_seen=set()\n        try:\n            _v4=_kairos_radar_after_htf_capture(tf_map,ts_corte,liquidity_policy=liquidity_policy)\n            if _v4.get('valid'):\n                _cap=_v4.get('capture') or {}; _st=_v4.get('structure') or {}\n                _key=(_cap.get('liquidity_tf'),_cap.get('sweep_ts'),_st.get('tf'),_st.get('close_ts'),_v4.get('direction'))\n                if _key not in _kairos_v4_seen:\n                    _kairos_v4_seen.add(_key)\n                    print(f\"[KAIROS_STRUCTURAL_RADAR_SIGNAL] pair={pair} direction={_v4.get('direction')} htf={_cap.get('liquidity_tf')} level={_cap.get('liquidity_level')} sweep_ts={_cap.get('sweep_ts')} structure_tf={_st.get('tf')} structure_type={_st.get('type')} structure_level={_st.get('level')} structure_close_ts={_st.get('close_ts')}\",flush=True)\n        except Exception as _e:\n            print(f'[KAIROS_HTF_STRUCTURE_V4_ERROR] pair={pair} error={_e}',flush=True)\n        try:\n            r = avaliar_vortex_decision_layer_v2(\n"""
    s=s.replace(anchor,runtime,1)
p.write_text(s,encoding='utf-8')
compile(s,'scalp_engine.py','exec')
assert '[KAIROS_STRUCTURAL_RADAR_SIGNAL]' in s
print('[KAIROS_HTF_STRUCTURE_V4] causal replay compile PASS')
