from pathlib import Path

p = Path('scalp_engine.py')
s = p.read_text(encoding='utf-8')

MARKER = '# KAIROS_STRUCTURAL_RADAR_V2'
if MARKER in s:
    print('[STRUCTURAL_RADAR_PATCH] already applied')
    raise SystemExit(0)

anchor = "def _paper_trading_v2_enviar_telegram(mensagem):\n"
if anchor not in s:
    raise SystemExit('[STRUCTURAL_RADAR_PATCH] telegram anchor not found')

block = r'''# KAIROS_STRUCTURAL_RADAR_V2
# Radar cedo: liquidez HTF -> first capture/reacao -> M15 major+internal.
# FVG/OB/reteste/entry/SL/TP sao metadata, nunca veto do alerta estrutural.
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


def _radar_meta(r):
    zone_type = r.get('zone_type') or r.get('poi_type') or r.get('tipo_zona')
    zone_top = r.get('zone_top') or r.get('poi_top') or r.get('zona_top')
    zone_bottom = r.get('zone_bottom') or r.get('poi_bottom') or r.get('zona_bottom')
    if zone_type or zone_top is not None or zone_bottom is not None:
        return f'POI causal: {zone_type or "N/A"} [{zone_bottom} - {zone_top}]'
    return 'POI causal: ainda pendente (nao veta o alerta)'


def _formatar_structural_radar(pair, r):
    ts=r.get('choch_timestamp')
    ts_txt=(datetime.fromtimestamp(ts/1000,tz=timezone.utc).strftime('%Y-%m-%d %H:%M UTC') if ts else 'N/A')
    return (
        f'🚨 <b>KAIROS — ALERTA ESTRUTURAL</b>\n'
        f'Par: {pair}\n'
        f'Direção: <b>{r.get("direction")}</b>\n'
        f'Liquidez: {r.get("liquidity_tf")} {r.get("liquidity_type")} @ {r.get("sweep_level")}\n'
        f'First capture / sweep: CONFIRMADO\n'
        f'Reação: {r.get("post_capture_state") or "CONFIRMADA"}\n'
        f'Estrutura M15: {r.get("choch_level")} @ {ts_txt}\n'
        f'M15 major + internal: CONFIRMADOS\n'
        f'{_radar_meta(r)}\n'
        f'Extremo sweep / referência SL: {r.get("sweep_extreme")}\n'
        f'⚠️ Radar estrutural; não é entrada automática.\n'
        f'Execução manual em DEMO.'
    )


def _paper_v2_tentar_structural_radar(db_file, pair, r, agora_ts_ms):
    if not r.get('intent_m15_found') or not r.get('direction') or not r.get('choch_timestamp'):
        return False
    if not _garantir_tabela_structural_radar(db_file):
        return False
    key=_structural_radar_key(pair,r)
    try:
        with sqlite3.connect(db_file) as conn:
            existe=conn.execute('SELECT 1 FROM kairos_structural_radar_alertas WHERE alert_key=?',(key,)).fetchone()
        if existe:
            return False

        # IMPORTANTE: Telegram primeiro. Se falhar, NAO grava dedup e tenta de novo no proximo tick.
        ok=_paper_trading_v2_enviar_telegram(_formatar_structural_radar(pair,r))
        if not ok:
            print(f'[KAIROS_STRUCTURAL_RADAR] pair={pair} sent=0 retry=1 key={key[:12]}',flush=True)
            return False

        with sqlite3.connect(db_file) as conn:
            conn.execute("""
                INSERT OR IGNORE INTO kairos_structural_radar_alertas
                (alert_key,pair,direction,liquidity_tf,liquidity_type,sweep_level,
                 first_capture_ts,structure_ts,structure_level,criado_em)
                VALUES (?,?,?,?,?,?,?,?,?,?)
            """,(key,pair,r.get('direction'),r.get('liquidity_tf'),r.get('liquidity_type'),
                  r.get('sweep_level'),r.get('first_capture_ts'),r.get('choch_timestamp'),
                  r.get('choch_level'),agora_ts_ms))
            conn.commit()
        print(f'[KAIROS_STRUCTURAL_RADAR] pair={pair} new=1 sent=1 key={key[:12]} dir={r.get("direction")} structure_ts={r.get("choch_timestamp")}',flush=True)
        return True
    except Exception as exc:
        print(f'[KAIROS_STRUCTURAL_RADAR] pair={pair} error={exc}',flush=True)
        return False


'''
s = s.replace(anchor, block + anchor, 1)

needle = """            if not r['valid']:\n                motivo = r.get('failure_reason') or 'DESCONHECIDO'\n"""
replacement = """            # Radar estrutural somente no estado mais recente; dispara antes de\n            # POI/reteste/entry/SL/TP sem enfraquecer o motor completo.\n            if i == len(m5) - 1 and r.get('intent_m15_found'):\n                try:\n                    _paper_v2_tentar_structural_radar(db_file, pair, r, agora_ts_ms)\n                except Exception as e_radar:\n                    print(f'[KAIROS_STRUCTURAL_RADAR] falha isolada {pair}: {e_radar}', flush=True)\n\n            if not r['valid']:\n                motivo = r.get('failure_reason') or 'DESCONHECIDO'\n"""
if needle not in s:
    raise SystemExit('[STRUCTURAL_RADAR_PATCH] tick anchor not found')
s = s.replace(needle, replacement, 1)

p.write_text(s, encoding='utf-8')
compile(s, 'scalp_engine.py', 'exec')
assert MARKER in s
assert 'Telegram primeiro' in s
assert '_paper_v2_tentar_structural_radar(db_file, pair, r, agora_ts_ms)' in s
print('[STRUCTURAL_RADAR_PATCH] applied + compile PASS')
