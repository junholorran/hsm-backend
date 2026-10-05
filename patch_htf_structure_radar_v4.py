from pathlib import Path
p=Path('scalp_engine.py')
s=p.read_text(encoding='utf-8')
MARK='# KAIROS_HTF_STRUCTURE_RADAR_V4'
if MARK not in s:
    a="def _paper_trading_v2_enviar_telegram(mensagem):\n"
    if a not in s: raise SystemExit('anchor missing')
    b=r'''# KAIROS_HTF_STRUCTURE_RADAR_V4
# The manipulation anchor is the existing relevant W1/D1/H4/H1 capture.
# After that capture, M15 is primary structure confirmation; M5 is allowed refinement.
# No second M5 liquidity capture is required.
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
    return {'tf':tf,'direction':'LONG' if ev.get('direcao')=='alta' else 'SHORT','type':ev.get('tipo'),'level':ev.get('nivel'),'open_ts':ev.get('t'),'close_ts':ct}

def _kairos_radar_after_htf_capture(candles_por_tf, cutoff, liquidity_policy='A_CURRENT'):
    snap={}
    for k,t in (('MN','M'),('W1','W'),('D1','D'),('H4','240'),('H1','60'),('M15','15')):
        snap[k]=_kairos_candles_fechados_ate(candles_por_tf.get(k) or [],t,cutoff)
    cap,audit=_kairos_select_structural_first_capture_sweep(snap,cutoff,liquidity_policy=liquidity_policy)
    if not cap: return {'valid':False,'reason':'NO_RELEVANT_HTF_CAPTURE','audit':audit}
    if cap.get('liquidity_tf') not in ('W1','D1','H4','H1'):
        return {'valid':False,'reason':'CAPTURE_NOT_PRIMARY_HTF','capture':cap,'audit':audit}
    st=cap.get('sweep_ts')
    c15=_kairos_candles_fechados_ate(candles_por_tf.get('M15') or [],'15',cutoff)
    c5=_kairos_candles_fechados_ate(candles_por_tf.get('M5') or [],'5',cutoff)
    e15=_kairos_structure_confirmation_after_capture(c15,st,50,'15')
    e5=_kairos_structure_confirmation_after_capture(c5,st,5,'5')
    chosen=e15 or e5
    if not chosen: return {'valid':False,'reason':'WAIT_STRUCTURE_BREAK_AFTER_HTF_CAPTURE','capture':cap,'audit':audit}
    return {'valid':True,'direction':chosen['direction'],'structure':chosen,'m15':e15,'m5':e5,'capture':cap,'audit':audit}

'''
    s=s.replace(a,b+a,1)
    ra="    sinais_unicos = []\n    chaves_vistas = set()\n"
    rb="""    # KAIROS_HTF_STRUCTURE_RADAR_V4 audit\n    _v4=_kairos_radar_after_htf_capture({'MN':mn,'W1':w1,'D1':d1,'H4':h4,'H1':h1,'M15':m15,'M5':m5},fim_ts_ms,liquidity_policy=liquidity_policy)\n    print(f'[KAIROS_HTF_STRUCTURE_V4] pair={pair} result={_v4}',flush=True)\n\n    sinais_unicos = []\n    chaves_vistas = set()\n"""
    if ra not in s: raise SystemExit('replay anchor missing')
    s=s.replace(ra,rb,1)
p.write_text(s,encoding='utf-8')
compile(s,'scalp_engine.py','exec')
assert MARK in s
assert 'WAIT_STRUCTURE_BREAK_AFTER_HTF_CAPTURE' in s
print('[KAIROS_HTF_STRUCTURE_V4] compile PASS')
