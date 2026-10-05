from pathlib import Path

p = Path('scalp_engine.py')
s = p.read_text(encoding='utf-8')

# Keep the live structural radar patch, then add replay-only observability.
MARKER = '# KAIROS_STRUCTURAL_RADAR_V3'

# --- live radar (early structural alert; downstream execution gates never veto it) ---
if '# KAIROS_STRUCTURAL_RADAR_V2' not in s:
    anchor = "def _paper_trading_v2_enviar_telegram(mensagem):\n"
    if anchor not in s:
        raise SystemExit('[STRUCTURAL_RADAR_PATCH] telegram anchor not found')
    block = r'''# KAIROS_STRUCTURAL_RADAR_V2
# Radar cedo: liquidez HTF -> first capture/reacao -> M15 major+internal.
# FVG/OB/reteste/entry/SL/TP sao metadata, nunca veto do alerta estrutural.
def _garantir_tabela_structural_radar(db_file):
    try:
        with sqlite3.connect(db_file) as conn:
            conn.execute("""CREATE TABLE IF NOT EXISTS kairos_structural_radar_alertas (
                alert_key TEXT PRIMARY KEY, pair TEXT NOT NULL, direction TEXT NOT NULL,
                liquidity_tf TEXT, liquidity_type TEXT, sweep_level REAL,
                first_capture_ts INTEGER NOT NULL, structure_ts INTEGER NOT NULL,
                structure_level REAL, criado_em INTEGER NOT NULL)""")
            conn.commit()
        return True
    except Exception as exc:
        print(f'[KAIROS_STRUCTURAL_RADAR] db_error={exc}', flush=True); return False

def _structural_radar_key(pair, r):
    raw='|'.join(str(x) for x in (pair,r.get('liquidity_tf'),r.get('liquidity_type'),r.get('sweep_level'),r.get('first_capture_ts'),r.get('direction'),r.get('choch_timestamp'),r.get('choch_level')))
    return hashlib.sha256(raw.encode('utf-8')).hexdigest()

def _radar_meta(r):
    zt=r.get('zone_type') or r.get('poi_type') or r.get('tipo_zona'); top=r.get('zone_top') or r.get('poi_top') or r.get('zona_top'); bot=r.get('zone_bottom') or r.get('poi_bottom') or r.get('zona_bottom')
    return f'POI causal: {zt or "N/A"} [{bot} - {top}]' if zt or top is not None or bot is not None else 'POI causal: ainda pendente (nao veta o alerta)'

def _formatar_structural_radar(pair, r):
    ts=r.get('choch_timestamp'); txt=(datetime.fromtimestamp(ts/1000,tz=timezone.utc).strftime('%Y-%m-%d %H:%M UTC') if ts else 'N/A')
    return (f'🚨 <b>KAIROS — ALERTA ESTRUTURAL</b>\nPar: {pair}\nDireção: <b>{r.get("direction")}</b>\nLiquidez: {r.get("liquidity_tf")} {r.get("liquidity_type")} @ {r.get("sweep_level")}\nFirst capture / sweep: CONFIRMADO\nReação: {r.get("post_capture_state") or "CONFIRMADA"}\nEstrutura M15: {r.get("choch_level")} @ {txt}\nM15 major + internal: CONFIRMADOS\n{_radar_meta(r)}\nExtremo sweep / referência SL: {r.get("sweep_extreme")}\n⚠️ Radar estrutural; não é entrada automática.\nExecução manual em DEMO.')

def _paper_v2_tentar_structural_radar(db_file, pair, r, agora_ts_ms):
    if not r.get('intent_m15_found') or not r.get('direction') or not r.get('choch_timestamp'): return False
    if not _garantir_tabela_structural_radar(db_file): return False
    key=_structural_radar_key(pair,r)
    try:
        with sqlite3.connect(db_file) as conn:
            if conn.execute('SELECT 1 FROM kairos_structural_radar_alertas WHERE alert_key=?',(key,)).fetchone(): return False
        ok=_paper_trading_v2_enviar_telegram(_formatar_structural_radar(pair,r))
        if not ok:
            print(f'[KAIROS_STRUCTURAL_RADAR] pair={pair} sent=0 retry=1 key={key[:12]}',flush=True); return False
        with sqlite3.connect(db_file) as conn:
            conn.execute('INSERT OR IGNORE INTO kairos_structural_radar_alertas (alert_key,pair,direction,liquidity_tf,liquidity_type,sweep_level,first_capture_ts,structure_ts,structure_level,criado_em) VALUES (?,?,?,?,?,?,?,?,?,?)',(key,pair,r.get('direction'),r.get('liquidity_tf'),r.get('liquidity_type'),r.get('sweep_level'),r.get('first_capture_ts'),r.get('choch_timestamp'),r.get('choch_level'),agora_ts_ms)); conn.commit()
        print(f'[KAIROS_STRUCTURAL_RADAR] pair={pair} new=1 sent=1 key={key[:12]} dir={r.get("direction")}',flush=True); return True
    except Exception as exc:
        print(f'[KAIROS_STRUCTURAL_RADAR] pair={pair} error={exc}',flush=True); return False

'''
    s=s.replace(anchor,block+anchor,1)
    needle="""            if not r['valid']:\n                motivo = r.get('failure_reason') or 'DESCONHECIDO'\n"""
    replacement="""            if i == len(m5) - 1 and r.get('intent_m15_found'):\n                try:\n                    _paper_v2_tentar_structural_radar(db_file, pair, r, agora_ts_ms)\n                except Exception as e_radar:\n                    print(f'[KAIROS_STRUCTURAL_RADAR] falha isolada {pair}: {e_radar}', flush=True)\n\n            if not r['valid']:\n                motivo = r.get('failure_reason') or 'DESCONHECIDO'\n"""
    if needle not in s: raise SystemExit('[STRUCTURAL_RADAR_PATCH] tick anchor not found')
    s=s.replace(needle,replacement,1)

# --- replay: show raw M15 major structure signals FIRST; liquidity is classification AFTER ---
if MARKER not in s:
    replay_anchor="""    sinais_unicos = []\n    chaves_vistas = set()\n"""
    replay_block="""    # KAIROS_STRUCTURAL_RADAR_V3\n    raw_m15_structure_signals=[]\n    for _ev in compute_lux_structure_events(m15, swing_size=50):\n        _open_ts=_ev.get('t')\n        if _open_ts is None:\n            continue\n        _close_ts=_kairos_candle_close_ts(_open_ts,'15')\n        if _close_ts is None or _close_ts < inicio_ts_ms or _close_ts > fim_ts_ms:\n            continue\n        _snap={\n            'MN':_kairos_candles_fechados_ate(mn,'M',_close_ts),\n            'W1':_kairos_candles_fechados_ate(w1,'W',_close_ts),\n            'D1':_kairos_candles_fechados_ate(d1,'D',_close_ts),\n            'H4':_kairos_candles_fechados_ate(h4,'240',_close_ts),\n            'H1':_kairos_candles_fechados_ate(h1,'60',_close_ts),\n            'M15':_kairos_candles_fechados_ate(m15,'15',_close_ts),\n        }\n        _cap,_liq_audit=_kairos_select_structural_first_capture_sweep(_snap,_close_ts,liquidity_policy=liquidity_policy)\n        _after=bool(_cap and _cap.get('sweep_ts') is not None and _open_ts > _cap.get('sweep_ts'))\n        raw_m15_structure_signals.append({\n            'direction':'LONG' if _ev.get('direcao')=='alta' else 'SHORT',\n            'structure_type':_ev.get('tipo'),'structure_level':_ev.get('nivel'),\n            'structure_open_ts':_open_ts,'structure_confirm_close_ts':_close_ts,\n            'strong_liquidity_before_signal':_after,\n            'liquidity':({'tf':_cap.get('liquidity_tf'),'type':_cap.get('liquidity_type'),'level':_cap.get('nivel'),'sweep_ts':_cap.get('sweep_ts'),'sweep_extreme':_cap.get('extremo'),'reaction':_cap.get('post_capture_state')} if _after else None)\n        })\n    raw_m15_structure_summary={\n        'total':len(raw_m15_structure_signals),\n        'LONG':sum(1 for x in raw_m15_structure_signals if x['direction']=='LONG'),\n        'SHORT':sum(1 for x in raw_m15_structure_signals if x['direction']=='SHORT'),\n        'with_strong_liquidity_before_signal':sum(1 for x in raw_m15_structure_signals if x['strong_liquidity_before_signal']),\n        'without_strong_liquidity_before_signal':sum(1 for x in raw_m15_structure_signals if not x['strong_liquidity_before_signal']),\n    }\n    print(f'[KAIROS_RAW_M15_SUMMARY] pair={pair} summary={raw_m15_structure_summary}', flush=True)\n    for _raw in raw_m15_structure_signals:\n        print(f'[KAIROS_RAW_M15_SIGNAL] pair={pair} dir={_raw.get("direction")} type={_raw.get("structure_type")} level={_raw.get("structure_level")} open_ts={_raw.get("structure_open_ts")} confirm_close_ts={_raw.get("structure_confirm_close_ts")} strong_liq={_raw.get("strong_liquidity_before_signal")} liquidity={_raw.get("liquidity")}', flush=True)\n\n    sinais_unicos = []\n    chaves_vistas = set()\n"""
    if replay_anchor not in s: raise SystemExit('[STRUCTURAL_RADAR_PATCH] replay anchor not found')
    s=s.replace(replay_anchor,replay_block,1)

    return_anchor="""        'total_sinais_unicos': len(sinais_unicos),\n        'auditoria_dedup': auditoria_dedup,\n"""
    return_repl="""        'total_sinais_unicos': len(sinais_unicos),\n        'raw_m15_structure_summary': raw_m15_structure_summary,\n        'raw_m15_structure_signals': raw_m15_structure_signals,\n        'auditoria_dedup': auditoria_dedup,\n"""
    if return_anchor not in s: raise SystemExit('[STRUCTURAL_RADAR_PATCH] replay return anchor not found')
    s=s.replace(return_anchor,return_repl,1)

    wrapper_anchor="""            'experimental_intent_gate_summary':r.get('experimental_intent_gate_summary'),\n        }\n"""
    wrapper_repl="""            'experimental_intent_gate_summary':r.get('experimental_intent_gate_summary'),\n            'raw_m15_structure_summary':r.get('raw_m15_structure_summary'),\n            'raw_m15_structure_signals':r.get('raw_m15_structure_signals'),\n        }\n"""
    if wrapper_anchor not in s: raise SystemExit('[STRUCTURAL_RADAR_PATCH] wrapper anchor not found')
    s=s.replace(wrapper_anchor,wrapper_repl,1)

p.write_text(s,encoding='utf-8')
compile(s,'scalp_engine.py','exec')
assert '# KAIROS_STRUCTURAL_RADAR_V2' in s
assert MARKER in s
assert "[KAIROS_RAW_M15_SUMMARY]" in s
assert "[KAIROS_RAW_M15_SIGNAL]" in s
assert "'raw_m15_structure_signals': raw_m15_structure_signals" in s
assert "'raw_m15_structure_signals':r.get('raw_m15_structure_signals')" in s
print('[STRUCTURAL_RADAR_PATCH] V3 LOGGING applied + compile PASS')
