# scalp_engine.py
# ─────────────────────────────────────────────────────────────────────────
# Motor de Scalp Ao Vivo — aditivo, não mexe em nada do cascade_engine.
#
# Fluxo (nesta ordem, igual foi validado com o Juninho):
#   1. Zona D1 (cluster de S/R, banda — não linha fina)
#   2. Killzone (London 07h-10h ou NY 13h-16h, horário Portugal) — GATE.
#      Fora da killzone, o ciclo roda e guarda estado, mas NUNCA sinaliza
#      entrada nem manda Telegram, mesmo que score bata 75.
#   3. Sweep — preço varre liquidez na borda da zona D1
#   4. CHoCH — no timeframe de EXECUÇÃO escolhido pelo utilizador
#      (M1/M5/M15), confirma virada de estrutura depois do sweep
#   5. Retorno à FVG/OB deixado pelo CHoCH — zona de entrada
#   6. Score determinístico (mesmo espírito do cascade: soma de pesos,
#      nunca "sensação"). Só sinaliza/alerta Telegram com score >= 75
#      E dentro da killzone.
#
# Tudo isolado em tabelas próprias (scalp_zone_state, scalp_signal_state),
# nunca toca nas tabelas do cascade_engine ou do app.py.
# ─────────────────────────────────────────────────────────────────────────

import sqlite3
import time
from datetime import datetime, timezone, timedelta

SCORE_THRESHOLD_SINAL = 75
TOLERANCIA_CLUSTER_PCT = 0.006   # 0.6% — mesma tolerância usada no cascade pra clusterizar toques
MIN_EVENTOS_BANDA = 2            # mínimo de toques pra uma banda D1 ser considerada válida
SWING_LOOKBACK = 5               # candles de cada lado pra confirmar swing high/low no TF de execução

# Portugal (Europe/Lisbon) — horário local aproximado, sem lib de timezone externa.
# UTC+0 no inverno, UTC+1 no verão (DST). Pra simplificar e evitar dependência
# externa, usamos UTC+1 fixo (cobre a maior parte do ano de trading ativo);
# a diferença de 1h no inverno é aceitável pra um gate informativo de killzone.
PT_UTC_OFFSET_HOURS = 1

KILLZONES = [
    {'nome': 'London', 'inicio_h': 7, 'fim_h': 10},
    {'nome': 'New York', 'inicio_h': 13, 'fim_h': 16},
]


def init_scalp_db(db_file):
    with sqlite3.connect(db_file) as conn:
        cursor = conn.cursor()
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS scalp_watch (
                pair TEXT PRIMARY KEY,
                exec_tf TEXT DEFAULT 'M5',
                enabled INTEGER DEFAULT 1,
                created_at INTEGER DEFAULT 0
            )
        ''')
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS scalp_zone_state (
                pair TEXT PRIMARY KEY,
                zona_top REAL,
                zona_bottom REAL,
                fase TEXT,
                sweep_ts INTEGER,
                sweep_nivel REAL,
                sweep_lado TEXT,
                choch_ts INTEGER,
                choch_nivel REAL,
                updated_at INTEGER
            )
        ''')
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS scalp_signal_state (
                id TEXT PRIMARY KEY,
                pair TEXT,
                created_at INTEGER,
                exec_tf TEXT,
                direcao TEXT,
                score INTEGER,
                entry REAL,
                sl REAL,
                tp REAL,
                na_killzone INTEGER,
                alerted INTEGER DEFAULT 0
            )
        ''')
        conn.commit()


def is_in_killzone(now_utc=None):
    """Retorna (bool, nome_killzone_ou_None). Horário Portugal, gate de scalp."""
    now_utc = now_utc or datetime.now(timezone.utc)
    pt_hour = (now_utc + timedelta(hours=PT_UTC_OFFSET_HOURS)).hour
    for kz in KILLZONES:
        if kz['inicio_h'] <= pt_hour < kz['fim_h']:
            return True, kz['nome']
    return False, None


# ── ZONA D1 (cluster de S/R) ────────────────────────────────────────────
def compute_d1_zones(d1_candles):
    """Agrupa highs/lows do D1 em bandas (cluster), igual à lógica do
    cascade_engine: tolerância percentual, mínimo de toques pra validar."""
    swings = []
    lb = 2
    for i in range(lb, len(d1_candles) - lb):
        c = d1_candles[i]
        is_high = all(c['h'] >= d1_candles[j]['h'] for j in range(i - lb, i + lb + 1) if j != i)
        is_low = all(c['l'] <= d1_candles[j]['l'] for j in range(i - lb, i + lb + 1) if j != i)
        if is_high:
            swings.append({'valor': c['h'], 'tipo': 'high'})
        if is_low:
            swings.append({'valor': c['l'], 'tipo': 'low'})

    grupos = []
    for s in swings:
        colocado = False
        for g in grupos:
            diff_pct = abs(s['valor'] - g['nivel']) / g['nivel']
            if diff_pct <= TOLERANCIA_CLUSTER_PCT:
                g['pontos'].append(s['valor'])
                g['nivel'] = sum(g['pontos']) / len(g['pontos'])
                colocado = True
                break
        if not colocado:
            grupos.append({'nivel': s['valor'], 'pontos': [s['valor']]})

    bandas = []
    for g in grupos:
        if len(g['pontos']) >= MIN_EVENTOS_BANDA:
            largura = g['nivel'] * TOLERANCIA_CLUSTER_PCT
            bandas.append({
                'top': g['nivel'] + largura,
                'bottom': g['nivel'] - largura,
                'toques': len(g['pontos']),
            })
    return bandas


def find_active_zone(bandas, preco_atual):
    for b in bandas:
        if b['bottom'] <= preco_atual <= b['top']:
            return b
    return None


# ── SWEEP + CHoCH no timeframe de execução ──────────────────────────────
def detect_exec_swings(exec_candles, lookback=SWING_LOOKBACK):
    swings = []
    for i in range(lookback, len(exec_candles) - lookback):
        c = exec_candles[i]
        window = exec_candles[i - lookback:i + lookback + 1]
        if all(c['h'] >= o['h'] for o in window if o is not c):
            swings.append({'index': i, 'tipo': 'high', 'valor': c['h']})
        if all(c['l'] <= o['l'] for o in window if o is not c):
            swings.append({'index': i, 'tipo': 'low', 'valor': c['l']})
    return swings


def detect_sweep_in_zone(exec_candles, zona):
    """Procura, nos candles mais recentes do TF de execução, um sweep da
    borda da zona D1 (varredura de liquidez acima/abaixo da banda)."""
    for i in range(len(exec_candles) - 1, max(0, len(exec_candles) - 30), -1):
        c = exec_candles[i]
        if c['h'] > zona['top'] and c['c'] < zona['top']:
            return {'index': i, 'lado': 'alta', 'nivel': c['h'], 't': c['t']}
        if c['l'] < zona['bottom'] and c['c'] > zona['bottom']:
            return {'index': i, 'lado': 'baixa', 'nivel': c['l'], 't': c['t']}
    return None


def detect_choch_after_sweep(exec_candles, sweep):
    """Depois do sweep, procura quebra de estrutura na direção oposta
    (CHoCH real, confirmado por fechamento, não só pavio)."""
    swings = detect_exec_swings(exec_candles)
    ref = None
    for s in swings:
        if s['index'] <= sweep['index']:
            continue
        # sweep de baixa (varreu SSL) -> espera CHoCH de alta (quebra de high anterior)
        if sweep['lado'] == 'baixa' and s['tipo'] == 'high':
            ref = s
            break
        # sweep de alta (varreu BSL) -> espera CHoCH de baixa (quebra de low anterior)
        if sweep['lado'] == 'alta' and s['tipo'] == 'low':
            ref = s
            break
    if not ref:
        return None

    for i in range(ref['index'] + 1, len(exec_candles)):
        c = exec_candles[i]
        if sweep['lado'] == 'baixa' and c['c'] > ref['valor']:
            return {'index': i, 'direcao': 'alta', 'nivel': ref['valor'], 't': c['t']}
        if sweep['lado'] == 'alta' and c['c'] < ref['valor']:
            return {'index': i, 'direcao': 'baixa', 'nivel': ref['valor'], 't': c['t']}
    return None


def find_fvg_ob_after_choch(exec_candles, choch):
    """Procura FVG (3 candles) ou Order Block (última vela contrária antes
    do impulso) formado pelo próprio movimento do CHoCH."""
    start = max(0, choch['index'] - 1)
    end = min(len(exec_candles) - 1, choch['index'] + 4)

    for i in range(start + 1, end):
        if i + 1 >= len(exec_candles):
            break
        prev, nxt = exec_candles[i - 1], exec_candles[i + 1]
        if choch['direcao'] == 'alta' and nxt['l'] > prev['h']:
            return {'tipo': 'FVG', 'top': nxt['l'], 'bottom': prev['h']}
        if choch['direcao'] == 'baixa' and nxt['h'] < prev['l']:
            return {'tipo': 'FVG', 'top': prev['l'], 'bottom': nxt['h']}

    # fallback: Order Block = última vela contrária antes do candle de CHoCH
    for i in range(choch['index'], max(0, choch['index'] - 6), -1):
        c = exec_candles[i]
        up = c['c'] >= c['o']
        if choch['direcao'] == 'alta' and not up:
            return {'tipo': 'OB', 'top': c['o'], 'bottom': c['c']}
        if choch['direcao'] == 'baixa' and up:
            return {'tipo': 'OB', 'top': c['c'], 'bottom': c['o']}
    return None


def price_in_zone(entry_zone, preco):
    return entry_zone['bottom'] <= preco <= entry_zone['top']


# ── RSI simples (mesmo cálculo padrão, período 14) ──────────────────────
def compute_rsi(closes, period=14):
    if len(closes) < period + 1:
        return [None] * len(closes)
    rsi = [None] * len(closes)
    gains = losses = 0.0
    for i in range(1, period + 1):
        diff = closes[i] - closes[i - 1]
        gains += max(diff, 0)
        losses += max(-diff, 0)
    avg_gain, avg_loss = gains / period, losses / period
    rsi[period] = 100 if avg_loss == 0 else 100 - (100 / (1 + avg_gain / avg_loss))
    for i in range(period + 1, len(closes)):
        diff = closes[i] - closes[i - 1]
        gain, loss = max(diff, 0), max(-diff, 0)
        avg_gain = (avg_gain * (period - 1) + gain) / period
        avg_loss = (avg_loss * (period - 1) + loss) / period
        rsi[i] = 100 if avg_loss == 0 else 100 - (100 / (1 + avg_gain / avg_loss))
    return rsi


def compute_score(zona, sweep, choch, entry_zone, exec_candles, na_killzone):
    """Soma de pesos determinística, mesmo espírito do cascade_engine.
    Retorna (score, detalhes[(nome, pontos), ...])."""
    detalhes = []
    score = 0

    # zona D1 — mais toques = zona mais respeitada
    pts_zona = 20 if zona['toques'] >= 3 else 15
    score += pts_zona
    detalhes.append(('banda_d1', pts_zona))

    # killzone — só soma se o sweep+CHoCH aconteceram dentro da killzone
    if na_killzone:
        score += 10
        detalhes.append(('dentro_killzone', 10))

    # sweep + CHoCH confirmados
    score += 25
    detalhes.append(('sweep_choch', 25))

    # retorno na FVG/OB — FVG pesa mais que OB (mais preciso)
    pts_fvg_ob = 20 if entry_zone['tipo'] == 'FVG' else 15
    score += pts_fvg_ob
    detalhes.append(('fvg_ob_retorno', pts_fvg_ob))

    # RSI no candle de entrada (extremo a favor soma mais)
    closes = [c['c'] for c in exec_candles]
    rsi_series = compute_rsi(closes)
    last_rsi = rsi_series[-1]
    if last_rsi is not None:
        if choch['direcao'] == 'alta' and last_rsi <= 40:
            pts_rsi = 12 if last_rsi <= 30 else 8
            score += pts_rsi
            detalhes.append(('rsi_favoravel', pts_rsi))
        elif choch['direcao'] == 'baixa' and last_rsi >= 60:
            pts_rsi = 12 if last_rsi >= 70 else 8
            score += pts_rsi
            detalhes.append(('rsi_favoravel', pts_rsi))

    # volume do candle de CHoCH acima da média recente = confirma força real
    vols = [c.get('v', 0) for c in exec_candles[-20:]]
    if vols:
        media_vol = sum(vols) / len(vols)
        choch_candle = exec_candles[choch['index']] if choch['index'] < len(exec_candles) else None
        if choch_candle and choch_candle.get('v', 0) > media_vol * 1.3:
            score += 8
            detalhes.append(('volume_choch_forte', 8))

    return min(score, 100), detalhes


def process_pair_scalp(db_file, pair, d1_candles, exec_candles, exec_tf_label, send_telegram_fn=None):
    """Orquestrador do ciclo de Scalp Ao Vivo pra 1 par. Aditivo, roda em
    cima dos mesmos candles já buscados no ciclo do Trade Ao Vivo — não
    faz nenhuma call extra à Bybit."""
    now = int(time.time())
    na_killzone, killzone_nome = is_in_killzone()
    preco_atual = exec_candles[-1]['c']

    bandas = compute_d1_zones(d1_candles)
    zona = find_active_zone(bandas, preco_atual)

    resultado = {
        'pair': pair,
        'exec_tf': exec_tf_label,
        'na_killzone': na_killzone,
        'killzone_nome': killzone_nome,
        'score': 0,
        'direcao': None,
        'entry': None,
        'sl': None,
        'tp': None,
        'motivo': None,
        'detalhes': [],
        'zona_top': None,
        'zona_bottom': None,
        'sweep_nivel': None,
        'sweep_lado': None,
        'choch_nivel': None,
        'choch_direcao': None,
        'entry_zone_top': None,
        'entry_zone_bottom': None,
    }

    if not zona:
        resultado['motivo'] = 'preço fora de qualquer banda D1 válida'
        _save_zone_state(db_file, pair, None, 'idle', now)
        return resultado

    resultado['zona_top'] = round(zona['top'], 6)
    resultado['zona_bottom'] = round(zona['bottom'], 6)

    sweep = detect_sweep_in_zone(exec_candles, zona)
    if not sweep:
        resultado['motivo'] = 'sem sweep detectado ainda'
        _save_zone_state(db_file, pair, zona, 'zona', now)
        return resultado

    resultado['sweep_nivel'] = round(sweep['nivel'], 6)
    resultado['sweep_lado'] = sweep['lado']

    choch = detect_choch_after_sweep(exec_candles, sweep)
    if not choch:
        resultado['motivo'] = 'sweep ok, mas CHoCH ainda não confirmou'
        _save_zone_state(db_file, pair, zona, 'sweep', now, sweep=sweep)
        return resultado

    resultado['choch_nivel'] = round(choch['nivel'], 6)
    resultado['choch_direcao'] = choch['direcao']

    entry_zone = find_fvg_ob_after_choch(exec_candles, choch)
    if not entry_zone:
        resultado['motivo'] = 'CHoCH confirmado, mas sem FVG/OB de retorno ainda'
        _save_zone_state(db_file, pair, zona, 'choch', now, sweep=sweep, choch=choch)
        return resultado

    resultado['entry_zone_top'] = round(entry_zone['top'], 6)
    resultado['entry_zone_bottom'] = round(entry_zone['bottom'], 6)

    if not price_in_zone(entry_zone, preco_atual):
        resultado['motivo'] = 'preço ainda fora da zona de entrada (FVG/OB) — aguardando retorno'
        _save_zone_state(db_file, pair, zona, 'aguardando_retorno', now, sweep=sweep, choch=choch)
        return resultado

    score, detalhes = compute_score(zona, sweep, choch, entry_zone, exec_candles, na_killzone)
    resultado['score'] = score
    resultado['detalhes'] = detalhes
    resultado['direcao'] = choch['direcao']

    sl = sweep['nivel']
    entry = preco_atual
    risco = abs(entry - sl)
    tp = entry + risco * 2 if choch['direcao'] == 'alta' else entry - risco * 2
    resultado['entry'] = round(entry, 6)
    resultado['sl'] = round(sl, 6)
    resultado['tp'] = round(tp, 6)

    if score >= SCORE_THRESHOLD_SINAL:
        resultado['motivo'] = 'entrada' if na_killzone else 'score válido, mas fora da killzone — sem alerta'
        _save_zone_state(db_file, pair, zona, 'entrada', now, sweep=sweep, choch=choch)
        if na_killzone:
            _save_signal(db_file, pair, exec_tf_label, resultado, alerted=True)
            if send_telegram_fn:
                arrow = '📈' if choch['direcao'] == 'alta' else '📉'
                msg = f"⚡ <b>Sinal Scalp Ao Vivo — {pair}</b>\n\n"
                msg += f"{arrow} <b>{'LONG' if choch['direcao']=='alta' else 'SHORT'}</b> | TF execução: {exec_tf_label} | Killzone: {killzone_nome}\n"
                msg += f"📍 Entrada: {resultado['entry']}\n"
                msg += f"🛑 Stop: {resultado['sl']}\n"
                msg += f"✅ TP: {resultado['tp']}\n"
                msg += f"🎯 Score: {score}/100\n"
                msg += f"\n💡 Zona D1 → Sweep → CHoCH → retorno {entry_zone['tipo']}"
                send_telegram_fn(msg)
        else:
            _save_signal(db_file, pair, exec_tf_label, resultado, alerted=False)
    else:
        resultado['motivo'] = f'score {score} abaixo de {SCORE_THRESHOLD_SINAL} — sem entrada'
        _save_zone_state(db_file, pair, zona, 'score_insuficiente', now, sweep=sweep, choch=choch)

    return resultado


def _save_zone_state(db_file, pair, zona, fase, now, sweep=None, choch=None):
    try:
        with sqlite3.connect(db_file) as conn:
            cursor = conn.cursor()
            cursor.execute('''
                INSERT INTO scalp_zone_state (pair, zona_top, zona_bottom, fase, sweep_ts, sweep_nivel, sweep_lado, choch_ts, choch_nivel, updated_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(pair) DO UPDATE SET
                    zona_top=excluded.zona_top, zona_bottom=excluded.zona_bottom, fase=excluded.fase,
                    sweep_ts=excluded.sweep_ts, sweep_nivel=excluded.sweep_nivel, sweep_lado=excluded.sweep_lado,
                    choch_ts=excluded.choch_ts, choch_nivel=excluded.choch_nivel, updated_at=excluded.updated_at
            ''', (
                pair,
                zona['top'] if zona else None, zona['bottom'] if zona else None,
                fase,
                sweep['t'] if sweep else None, sweep['nivel'] if sweep else None, sweep['lado'] if sweep else None,
                choch['t'] if choch else None, choch['nivel'] if choch else None,
                now,
            ))
            conn.commit()
    except Exception as e:
        print(f"[scalp_engine] erro ao salvar zone_state de {pair}: {e}")


def _save_signal(db_file, pair, exec_tf_label, resultado, alerted):
    try:
        signal_id = f"scalp_{pair}_{int(time.time()*1000)}"
        with sqlite3.connect(db_file) as conn:
            cursor = conn.cursor()
            cursor.execute('''
                INSERT INTO scalp_signal_state (id, pair, created_at, exec_tf, direcao, score, entry, sl, tp, na_killzone, alerted)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ''', (
                signal_id, pair, int(time.time()), exec_tf_label,
                resultado['direcao'], resultado['score'], resultado['entry'], resultado['sl'], resultado['tp'],
                1 if resultado['na_killzone'] else 0, 1 if alerted else 0,
            ))
            conn.commit()
    except Exception as e:
        print(f"[scalp_engine] erro ao salvar signal de {pair}: {e}")
