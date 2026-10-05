from pathlib import Path

p = Path('scalp_engine.py')
s = p.read_text(encoding='utf-8')

MARKER = '# KAIROS_STRUCTURAL_RADAR_V1'
if MARKER in s:
    print('[STRUCTURAL_RADAR_PATCH] already applied')
    raise SystemExit(0)

anchor = "def _paper_trading_v2_enviar_telegram(mensagem):\n"
if anchor not in s:
    raise SystemExit('[STRUCTURAL_RADAR_PATCH] telegram anchor not found')

block = r'''# KAIROS_STRUCTURAL_RADAR_V1
# Alerta cedo: liquidez HTF relevante -> first capture/reacao resolvida ->
# estrutura M15 major+internal confirmada. POI/reteste/entry/SL/TP nao vetam.
def _garantir_tabela_structural_radar(db_file):
    try:
        with sqlite3.connect(db_file) as conn:
            conn.execute("""
                CREATE TABLE IF NOT EXISTS kairos_structural_radar_alertas (
                    alert_key TEXT PRIMARY KEY,
                    pair TEXT NOT NULL,
                    direction TEXT NOT NULL,
                    liquidity_tf TEXT,
                    liquidity_type TEXT,
                    sweep_level REAL,
                    first_capture_ts INTEGER NOT NULL,
                    structure_ts INTEGER NOT NULL,
                    structure_level REAL,
                    criado_em INTEGER NOT NULL
                )
            """)
            conn.commit()
        return True
    except Exception as exc:
        print(f'[KAIROS_STRUCTURAL_RADAR] db_error={exc}', flush=True)
        return False


def _structural_radar_key(pair, r):
    raw='|'.join(str(x) for x in (
        pair, r.get('liquidity_tf'), r.get('liquidity_type'), r.get('sweep_level'),
        r.get('first_capture_ts'), r.get('direction'), r.get('choch_timestamp'), r.get('choch_level')
    ))
    return hashlib.sha256(raw.encode('utf-8')).hexdigest()


def _formatar_structural_radar(pair, r):
    ts=r.get('choch_timestamp')
    ts_txt=(datetime.fromtimestamp(ts/1000,tz=timezone.utc).strftime('%Y-%m-%d %H:%M UTC') if ts else 'N/A')
    return (
        f'🚨 <b>KAIROS — ALERTA ESTRUTURAL</b>\n'
        f'Par: {pair}\n'
        f'Direção: <b>{r.get("direction")}</b>\n'
        f'Liquidez: {r.get("liquidity_tf")} {r.get("liquidity_type")} @ {r.get("sweep_level")}\n'
        f'First capture / sweep: CONFIRMADO\n'
        f'Reação: CONFIRMADA\n'
        f'Estrutura M15: {r.get("choch_level")} @ {ts_txt}\n'
        f'M15 major + internal: CONFIRMADOS\n'
        f'Extremo sweep: {r.get("sweep_extreme")}\n'
        f'⚠️ Radar estrutural. FVG/OB/reteste/entry/SL/TP ainda podem estar pendentes.\n'
        f'Execução manual em DEMO.'
    )


def _paper_v2_tentar_structural_radar(db_file, pair, r, agora_ts_ms):
    # O motor so marca intent_m15_found depois de captura estrutural valida,
    # reacao resolvida e major50+internal5 M15 concordarem causalmente.
    if not r.get('intent_m15_found') or not r.get('direction') or not r.get('choch_timestamp'):
        return False
    if not _garantir_tabela_structural_radar(db_file):
        return False
    key=_structural_radar_key(pair,r)
    try:
        with sqlite3.connect(db_file) as conn:
            cur=conn.execute("""
                INSERT OR IGNORE INTO kairos_structural_radar_alertas
                (alert_key,pair,direction,liquidity_tf,liquidity_type,sweep_level,
                 first_capture_ts,structure_ts,structure_level,criado_em)
                VALUES (?,?,?,?,?,?,?,?,?,?)
            """,(key,pair,r.get('direction'),r.get('liquidity_tf'),r.get('liquidity_type'),
                  r.get('sweep_level'),r.get('first_capture_ts'),r.get('choch_timestamp'),
                  r.get('choch_level'),agora_ts_ms))
            conn.commit()
            novo=cur.rowcount>0
        if not novo:
            return False
        ok=_paper_trading_v2_enviar_telegram(_formatar_structural_radar(pair,r))
        print(f'[KAIROS_STRUCTURAL_RADAR] pair={pair} new=1 sent={bool(ok)} key={key[:12]} dir={r.get("direction")} structure_ts={r.get("choch_timestamp")}',flush=True)
        return bool(ok)
    except Exception as exc:
        print(f'[KAIROS_STRUCTURAL_RADAR] pair={pair} error={exc}',flush=True)
        return False


'''
s = s.replace(anchor, block + anchor, 1)

needle = """            if not r['valid']:\n                motivo = r.get('failure_reason') or 'DESCONHECIDO'\n"""
replacement = """            # Radar estrutural: somente o estado MAIS RECENTE. Dispara antes dos\n            # gates de POI/reteste/entry/SL/TP, sem enfraquecer o motor completo.\n            if i == len(m5) - 1 and r.get('intent_m15_found'):\n                try:\n                    _paper_v2_tentar_structural_radar(db_file, pair, r, agora_ts_ms)\n                except Exception as e_radar:\n                    print(f'[KAIROS_STRUCTURAL_RADAR] falha isolada {pair}: {e_radar}', flush=True)\n\n            if not r['valid']:\n                motivo = r.get('failure_reason') or 'DESCONHECIDO'\n"""
if needle not in s:
    raise SystemExit('[STRUCTURAL_RADAR_PATCH] tick anchor not found')
s = s.replace(needle, replacement, 1)

p.write_text(s, encoding='utf-8')

# Verificacao estatica minima do patch antes do deploy.
compile(s, 'scalp_engine.py', 'exec')
assert MARKER in s
assert '_paper_v2_tentar_structural_radar(db_file, pair, r, agora_ts_ms)' in s
assert "if i == len(m5) - 1 and r.get('intent_m15_found')" in s
print('[STRUCTURAL_RADAR_PATCH] applied + compile PASS')
