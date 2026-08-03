"""
cascade_engine.py
==================
Módulo ADITIVO para o Kairos Mentor. NÃO importa nem modifica nada do
app.py — só usa a mesma DB_FILE (SQLite) e a mesma função send_telegram
que já existem lá, passadas por parâmetro.

O que faz nesta primeira etapa:
  1. Calcula bandas de Suporte/Resistência Diário (cluster-based, com
     agrupamento por "eventos" e não por vela crua — ver conversa).
  2. Guarda o estado (bandas + se o preço está dentro delas) no SQLite.
  3. Dispara um alerta de Telegram SEPARADO ("🔔 Alerta de Zona") sempre
     que o preço ENTRA numa banda válida — sem exigir sweep/CHoCH, sem
     custo de IA, sem duplicar mensagem enquanto o preço continuar lá
     dentro.

Zero chamadas à Claude API aqui — é tudo matemática em cima dos candles
D1 que a Bybit já devolve.

Como integrar no app.py (3 linhas, ver bloco de instruções no fim do
arquivo):
  1. import cascade_engine
  2. cascade_engine.init_cascade_db(DB_FILE)
  3. dentro de run_live_cycle, depois de buscar os candles D1, chamar
     cascade_engine.process_pair(...)
"""

import sqlite3
import time


# ─── CONFIGURAÇÃO (ajustável sem mexer na lógica) ──────────────────────────

TOLERANCIA_CLUSTER_PCT = 0.006   # 0,6% — distância máx. entre pivôs pro mesmo cluster
MIN_EVENTOS_BANDA = 2            # mínimo de "visitas" separadas no tempo pra banda valer
JANELA_PIVO = 3                  # candles antes/depois pra confirmar um pivô
LOOKBACK_CANDLES_D1 = 180        # quantos candles D1 olhar pra trás
GAP_MINIMO_ENTRE_EVENTOS = 2     # candles fora da banda pra contar como "novo evento"


# ─── SETUP DA TABELA (aditivo, não mexe nas tabelas existentes) ───────────

def init_cascade_db(db_file):
    with sqlite3.connect(db_file) as conn:
        cursor = conn.cursor()
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS sr_zone_state (
                pair TEXT PRIMARY KEY,
                zone_min REAL,
                zone_max REAL,
                zone_type TEXT,
                eventos INTEGER,
                in_zone INTEGER DEFAULT 0,
                updated_at INTEGER
            )
        ''')
        conn.commit()


# ─── PASSO 1: ACHAR PIVÔS ───────────────────────────────────────────────

def _achar_pivos(candles, janela=JANELA_PIVO):
    pivos = []
    n = len(candles)
    for i in range(janela, n - janela):
        vizinhos = candles[i - janela:i + janela + 1]
        if candles[i]['h'] == max(c['h'] for c in vizinhos):
            pivos.append({'idx': i, 'tipo': 'resistencia', 'preco': candles[i]['h']})
        if candles[i]['l'] == min(c['l'] for c in vizinhos):
            pivos.append({'idx': i, 'tipo': 'suporte', 'preco': candles[i]['l']})
    return pivos


# ─── PASSO 2: AGRUPAR PIVÔS EM CLUSTERS (bandas) ────────────────────────

def _agrupar_em_bandas(pivos, tolerancia_pct=TOLERANCIA_CLUSTER_PCT):
    if not pivos:
        return []
    pivos_ordenados = sorted(pivos, key=lambda p: p['preco'])
    bandas = []
    atual = [pivos_ordenados[0]]

    for p in pivos_ordenados[1:]:
        centro = sum(x['preco'] for x in atual) / len(atual)
        if abs(p['preco'] - centro) / centro <= tolerancia_pct:
            atual.append(p)
        else:
            bandas.append(atual)
            atual = [p]
    bandas.append(atual)

    resultado = []
    for grupo in bandas:
        precos = [g['preco'] for g in grupo]
        resultado.append({
            'min': min(precos),
            'max': max(precos),
            'tipo': grupo[0]['tipo'],
            'pivos_idx': sorted(g['idx'] for g in grupo),
        })
    return resultado


# ─── PASSO 3: CONTAR EVENTOS (não velas cruas) ──────────────────────────

def _contar_eventos(pivos_idx, gap_minimo=GAP_MINIMO_ENTRE_EVENTOS):
    """
    Pivôs cujo índice está muito próximo (mesma 'visita') contam como
    1 evento só. Só conta novo evento se houver um gap real de candles
    entre uma visita e outra.
    """
    if not pivos_idx:
        return 0
    eventos = 1
    for i in range(1, len(pivos_idx)):
        if pivos_idx[i] - pivos_idx[i - 1] > gap_minimo:
            eventos += 1
    return eventos


# ─── FUNÇÃO PRINCIPAL: calcular bandas válidas do dia ───────────────────

def calcular_bandas_sr_diario(candles_d1, min_eventos=MIN_EVENTOS_BANDA):
    """
    candles_d1: lista de dicts no MESMO formato que fetch_bybit_klines já
    devolve no app.py -> [{'t':..,'o':..,'h':..,'l':..,'c':..,'v':..}, ...]
    (ordem cronológica, mais antigo primeiro — já é assim no app.py)
    """
    candles = candles_d1[-LOOKBACK_CANDLES_D1:]
    pivos = _achar_pivos(candles)
    bandas = _agrupar_em_bandas(pivos)

    bandas_validas = []
    for b in bandas:
        n_eventos = _contar_eventos(b['pivos_idx'])
        if n_eventos >= min_eventos:
            bandas_validas.append({
                'min': round(b['min'], 4),
                'max': round(b['max'], 4),
                'tipo': b['tipo'],
                'eventos': n_eventos,
            })
    return bandas_validas


def banda_mais_relevante(bandas, preco_atual):
    """
    De todas as bandas válidas, acha a que o preço está dentro agora
    (se houver) ou a mais próxima.
    """
    dentro = [b for b in bandas if b['min'] <= preco_atual <= b['max']]
    if dentro:
        return max(dentro, key=lambda b: b['eventos']), True
    if not bandas:
        return None, False
    mais_proxima = min(
        bandas,
        key=lambda b: min(abs(preco_atual - b['min']), abs(preco_atual - b['max']))
    )
    return mais_proxima, False


# ─── ALERTA DE ZONA (Telegram) — dedup por "evento de visita" ──────────

def _formatar_msg_zona(pair, banda):
    emoji = "🟥" if banda['tipo'] == 'resistencia' else "🟩"
    tipo_label = "RESISTÊNCIA" if banda['tipo'] == 'resistencia' else "SUPORTE"
    return (
        f"🔔 <b>{pair} entrou em zona de {tipo_label}</b>\n\n"
        f"{emoji} Banda D1: ${banda['min']:,.2f} – ${banda['max']:,.2f}\n"
        f"📊 Força: {banda['eventos']} eventos de reação\n\n"
        f"<i>Ainda sem sweep/CHoCH confirmado — só aviso de zona.</i>"
    )


def process_pair(db_file, pair, candles_d1, preco_atual, send_telegram_fn):
    """
    Chamar isso 1x por ciclo, por par, depois de buscar os candles D1.
    send_telegram_fn: passar a função send_telegram já existente no app.py.

    Retorna o dict com a banda ativa (ou None), útil pra já injetar no
    scoring/prompt mais pra frente, sem precisar recalcular de novo.
    """
    bandas = calcular_bandas_sr_diario(candles_d1)
    banda_ativa, esta_dentro = banda_mais_relevante(bandas, preco_atual)

    with sqlite3.connect(db_file) as conn:
        cursor = conn.cursor()
        cursor.execute('SELECT zone_min, zone_max, in_zone FROM sr_zone_state WHERE pair=?', (pair,))
        row = cursor.fetchone()
        estava_dentro = bool(row[2]) if row else False
        zona_anterior = (row[0], row[1]) if row else None

        agora = int(time.time())

        if esta_dentro and banda_ativa:
            zona_atual = (banda_ativa['min'], banda_ativa['max'])
            entrou_agora = (not estava_dentro) or (zona_atual != zona_anterior)

            if entrou_agora:
                send_telegram_fn(_formatar_msg_zona(pair, banda_ativa))

            cursor.execute('''
                INSERT INTO sr_zone_state (pair, zone_min, zone_max, zone_type, eventos, in_zone, updated_at)
                VALUES (?, ?, ?, ?, ?, 1, ?)
                ON CONFLICT(pair) DO UPDATE SET
                    zone_min=excluded.zone_min, zone_max=excluded.zone_max,
                    zone_type=excluded.zone_type, eventos=excluded.eventos,
                    in_zone=1, updated_at=excluded.updated_at
            ''', (pair, banda_ativa['min'], banda_ativa['max'], banda_ativa['tipo'], banda_ativa['eventos'], agora))
        else:
            cursor.execute('''
                INSERT INTO sr_zone_state (pair, zone_min, zone_max, zone_type, eventos, in_zone, updated_at)
                VALUES (?, NULL, NULL, NULL, 0, 0, ?)
                ON CONFLICT(pair) DO UPDATE SET in_zone=0, updated_at=excluded.updated_at
            ''', (pair, agora))

        conn.commit()

    return banda_ativa if esta_dentro else None


# ═══════════════════════════════════════════════════════════════════════
# FASE 2 — RESTO DA CASCATA (sweep, força do sweep, CHoCH, RSI,
# divergência, scoring, SL/TP e o Sinal Completo no Telegram).
# Tudo aqui embaixo também é aditivo — usa os candles M15 que o app.py
# já busca no loop de run_live_cycle, não pede nenhum dado novo à Bybit.
# ═══════════════════════════════════════════════════════════════════════

SCORE_THRESHOLD_SINAL = 75
RSI_PERIOD = 14


# ─── RSI (mesmo cálculo que já corrigiram no scoring da IA em julho) ──

def calcular_rsi(closes, period=RSI_PERIOD):
    if len(closes) < period + 1:
        return [None] * len(closes)

    rsis = [None] * len(closes)
    gains, losses = [], []
    for i in range(1, period + 1):
        diff = closes[i] - closes[i - 1]
        gains.append(max(diff, 0))
        losses.append(max(-diff, 0))
    avg_gain = sum(gains) / period
    avg_loss = sum(losses) / period
    rsis[period] = 100 - (100 / (1 + (avg_gain / avg_loss))) if avg_loss != 0 else 100

    for i in range(period + 1, len(closes)):
        diff = closes[i] - closes[i - 1]
        gain = max(diff, 0)
        loss = max(-diff, 0)
        avg_gain = (avg_gain * (period - 1) + gain) / period
        avg_loss = (avg_loss * (period - 1) + loss) / period
        rsis[i] = 100 - (100 / (1 + (avg_gain / avg_loss))) if avg_loss != 0 else 100

    return rsis


# ─── SWEEP: detectar varredura de liquidez no fim da série (M15) ──────

def detectar_sweep(candles_m15, banda, janela_lookback=20):
    """
    Olha as últimas `janela_lookback` velas M15 e verifica se alguma
    varreu (pavio) o limite da banda e fechou de volta pro lado oposto.
    Retorna (sweep_detectado: bool, idx_sweep: int|None, direcao: str|None)
    direcao 'alta' = varreu suporte pra cima (LONG) / 'baixa' = varreu
    resistência pra baixo (SHORT)
    """
    recentes = candles_m15[-janela_lookback:]
    offset = len(candles_m15) - len(recentes)

    for i, c in enumerate(recentes):
        if banda['tipo'] == 'suporte':
            varreu = c['l'] < banda['min']
            fechou_de_volta = c['c'] > banda['min']
            if varreu and fechou_de_volta:
                return True, offset + i, 'alta'
        else:  # resistencia
            varreu = c['h'] > banda['max']
            fechou_de_volta = c['c'] < banda['max']
            if varreu and fechou_de_volta:
                return True, offset + i, 'baixa'

    return False, None, None


# ─── FORÇA DO SWEEP (equal highs/lows, vela de impulso) ───────────────

def forca_do_sweep(candles_m15, idx_sweep, tolerancia_pct=0.0015):
    if idx_sweep is None or idx_sweep < 5:
        return {'forca': 'fraca', 'motivos': []}

    motivos = []
    janela = candles_m15[max(0, idx_sweep - 15):idx_sweep]

    highs = [c['h'] for c in janela]
    lows = [c['l'] for c in janela]
    for serie, nome in [(highs, 'equal_highs'), (lows, 'equal_lows')]:
        achou = False
        for i in range(len(serie)):
            for j in range(i + 1, len(serie)):
                if serie[i] == 0:
                    continue
                if abs(serie[i] - serie[j]) / serie[i] <= tolerancia_pct:
                    motivos.append(nome)
                    achou = True
                    break
            if achou:
                break

    if idx_sweep + 1 < len(candles_m15):
        vela = candles_m15[idx_sweep + 1]
        corpo = abs(vela['c'] - vela['o'])
        rng = vela['h'] - vela['l']
        if rng > 0 and (corpo / rng) > 0.7:
            motivos.append('vela_impulso')

    forca = 'forte' if motivos else 'fraca'
    return {'forca': forca, 'motivos': motivos}


# ─── CHoCH simplificado (M15): quebra de swing na direção do sweep ────

def detectar_choch(candles_m15, idx_sweep, direcao, janela=10):
    if idx_sweep is None:
        return False, None

    antes = candles_m15[max(0, idx_sweep - janela):idx_sweep]
    depois = candles_m15[idx_sweep:]
    if not antes or not depois:
        return False, None

    if direcao == 'alta':
        swing_ref = max(c['h'] for c in antes)
        for c in depois:
            if c['c'] > swing_ref:
                return True, swing_ref
    else:
        swing_ref = min(c['l'] for c in antes)
        for c in depois:
            if c['c'] < swing_ref:
                return True, swing_ref

    return False, None


# ─── DIVERGÊNCIA DE RSI (regra rígida: 2 pivôs comparáveis) ───────────

def detectar_divergencia_rsi(candles_m15, rsis, direcao, janela=30):
    recentes = candles_m15[-janela:]
    rsis_recentes = rsis[-janela:]

    pivos = []
    for i in range(2, len(recentes) - 2):
        if rsis_recentes[i] is None:
            continue
        if direcao == 'alta':
            if recentes[i]['l'] == min(c['l'] for c in recentes[i - 2:i + 3]):
                pivos.append((i, recentes[i]['l'], rsis_recentes[i]))
        else:
            if recentes[i]['h'] == max(c['h'] for c in recentes[i - 2:i + 3]):
                pivos.append((i, recentes[i]['h'], rsis_recentes[i]))

    if len(pivos) < 2:
        return False

    p1, p2 = pivos[-2], pivos[-1]
    if direcao == 'alta':
        return p2[1] < p1[1] and p2[2] > p1[2]
    else:
        return p2[1] > p1[1] and p2[2] < p1[2]


# ─── SCORING (a tabela de pesos que já fechamos) ──────────────────────

def calcular_score(banda, sweep_ok, choch_ok, forca_sweep_info, fvg_ob_presente,
                    rsi_atual, divergencia_ok, lateralizacao_previa=False):
    if not (banda and sweep_ok and choch_ok):
        return {'score': 0, 'detalhes': [], 'motivo': 'sem banda D1 + sweep + CHoCH'}

    score = 0
    detalhes = []

    score += 20
    detalhes.append(('banda_d1', 20))
    score += 25
    detalhes.append(('sweep_choch', 25))

    forca = forca_sweep_info['forca']
    if fvg_ob_presente:
        peso_fvg = 20 if forca == 'forte' else 8
        score += peso_fvg
        detalhes.append((f'fvg_ob_sweep_{forca}', peso_fvg))

    if rsi_atual is not None:
        if rsi_atual < 20 or rsi_atual > 80:
            score += 12
            detalhes.append(('rsi_extremo', 12))
        elif rsi_atual < 30 or rsi_atual > 70:
            score += 6
            detalhes.append(('rsi_sobre_c_v', 6))

    if divergencia_ok:
        score += 15
        detalhes.append(('divergencia_rsi', 15))

    if lateralizacao_previa:
        score += 8
        detalhes.append(('lateralizacao', 8))

    return {'score': min(score, 100), 'detalhes': detalhes, 'forca_sweep': forca}


# ─── SL/TP técnico ─────────────────────────────────────────────────────

def calcular_sl_tp(banda, direcao, forca_sweep_info, fvg_ob_zona=None,
                    proxima_zona_tecnica=None):
    if forca_sweep_info['forca'] == 'forte' and fvg_ob_zona:
        sl = fvg_ob_zona['min'] if direcao == 'alta' else fvg_ob_zona['max']
    else:
        sl = banda['min'] if direcao == 'alta' else banda['max']

    tp = proxima_zona_tecnica

    return {'sl': round(sl, 4), 'tp': round(tp, 4) if tp else None}


# ─── SINAL COMPLETO NO TELEGRAM (separado do Alerta de Zona) ──────────

def _formatar_msg_sinal(pair, direcao, score_info, entry, sl, tp):
    arrow = "📈" if direcao == "alta" else "📉"
    label = "LONG" if direcao == "alta" else "SHORT"
    emoji_score = "🟢" if score_info['score'] >= 85 else "🟡"
    msg = f"🚨 <b>Sinal Cascata — {pair}</b>\n\n"
    msg += f"{arrow} <b>{label}</b> {emoji_score} Score {score_info['score']}/100\n"
    if entry:
        msg += f"📍 Entrada: ${entry:,.4f}\n"
    if sl:
        msg += f"🛑 SL: ${sl:,.4f}\n"
    if tp:
        msg += f"✅ TP: ${tp:,.4f}\n"
    msg += "\n<b>Confluências:</b>\n"
    for nome, peso in score_info['detalhes']:
        msg += f"• {nome}: +{peso}\n"
    return msg


def init_cascade_signal_db(db_file):
    """Chamar junto com init_cascade_db() — tabela separada, também aditiva."""
    with sqlite3.connect(db_file) as conn:
        cursor = conn.cursor()
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS cascade_signal_state (
                pair TEXT PRIMARY KEY,
                last_signature TEXT,
                updated_at INTEGER
            )
        ''')
        conn.commit()


def _ja_alertado_hoje(db_file, pair, signature):
    with sqlite3.connect(db_file) as conn:
        cursor = conn.cursor()
        cursor.execute('SELECT last_signature FROM cascade_signal_state WHERE pair=?', (pair,))
        row = cursor.fetchone()
        return bool(row and row[0] == signature)


def _salvar_alerta(db_file, pair, signature):
    with sqlite3.connect(db_file) as conn:
        cursor = conn.cursor()
        cursor.execute('''
            INSERT INTO cascade_signal_state (pair, last_signature, updated_at)
            VALUES (?, ?, ?)
            ON CONFLICT(pair) DO UPDATE SET last_signature=excluded.last_signature, updated_at=excluded.updated_at
        ''', (pair, signature, int(time.time())))
        conn.commit()


# ─── ORQUESTRADOR COMPLETO — chamar isso 1x por ciclo, por par ────────

def process_pair_full(db_file, pair, candles_d1, candles_m15, send_telegram_fn,
                       fvg_ob_zona=None, proxima_zona_tecnica=None):
    """
    Roda a cascata inteira: zona -> sweep -> força -> CHoCH -> RSI ->
    divergência -> score -> (se >=75) Sinal completo no Telegram.

    candles_d1 / candles_m15: mesmo formato do fetch_bybit_klines do
    app.py, já buscados no loop de run_live_cycle (zero call extra à
    Bybit).
    fvg_ob_zona: opcional, dict {'min':.., 'max':..} — se ainda não
    tiver essa detecção pronta, passa None (o score simplesmente não
    soma essa camada, sem quebrar nada).
    proxima_zona_tecnica: opcional, preço da próxima banda pra virar TP.
    """
    preco_atual = candles_d1[-1]['c']

    banda_ativa = process_pair(db_file, pair, candles_d1, preco_atual, send_telegram_fn)
    if not banda_ativa:
        return {'score': 0, 'motivo': 'preço fora de qualquer banda D1 válida'}

    sweep_ok, idx_sweep, direcao = detectar_sweep(candles_m15, banda_ativa)
    if not sweep_ok:
        return {'score': 0, 'motivo': 'sem sweep detectado ainda'}

    forca_info = forca_do_sweep(candles_m15, idx_sweep)

    choch_ok, _ = detectar_choch(candles_m15, idx_sweep, direcao)
    if not choch_ok:
        return {'score': 0, 'motivo': 'sweep ok, mas CHoCH ainda não confirmou'}

    closes_m15 = [c['c'] for c in candles_m15]
    rsis = calcular_rsi(closes_m15)
    rsi_atual = rsis[-1]
    divergencia_ok = detectar_divergencia_rsi(candles_m15, rsis, direcao)

    score_info = calcular_score(
        banda_ativa, sweep_ok, choch_ok, forca_info,
        fvg_ob_presente=bool(fvg_ob_zona), rsi_atual=rsi_atual,
        divergencia_ok=divergencia_ok,
    )

    if score_info['score'] < SCORE_THRESHOLD_SINAL:
        return score_info

    sltp = calcular_sl_tp(banda_ativa, direcao, forca_info, fvg_ob_zona, proxima_zona_tecnica)
    signature = f"{direcao}|{round(preco_atual, 2)}|{score_info['score']}"

    if not _ja_alertado_hoje(db_file, pair, signature):
        msg = _formatar_msg_sinal(pair, direcao, score_info, preco_atual, sltp['sl'], sltp['tp'])
        send_telegram_fn(msg)
        _salvar_alerta(db_file, pair, signature)

    score_info['sl'] = sltp['sl']
    score_info['tp'] = sltp['tp']
    score_info['direcao'] = direcao
    return score_info


# ═══════════════════════════════════════════════════════════════════════
# FASE 3 — S/R em H4 + orquestrador multi-timeframe (D1 + H4, com
# confirmação de sweep/CHoCH no M15). 100% aditivo, não altera nada
# das fases anteriores.
# ═══════════════════════════════════════════════════════════════════════

# ─── S/R baseado em H4 (mesma lógica do D1, candles diferentes) ────────

def calcular_bandas_sr_h4(candles_h4, min_eventos=MIN_EVENTOS_BANDA, lookback=480):
    """
    Mesma lógica de calcular_bandas_sr_diario, aplicada ao H4.
    lookback=480 candles H4 (~80 dias) pra dar contexto parecido ao D1.
    Ajuste esse número se quiser zonas mais recentes (lookback menor)
    ou mais antigas também contando (lookback maior).
    """
    candles = candles_h4[-lookback:]
    pivos = _achar_pivos(candles)
    bandas = _agrupar_em_bandas(pivos)

    bandas_validas = []
    for b in bandas:
        n_eventos = _contar_eventos(b['pivos_idx'])
        if n_eventos >= min_eventos:
            bandas_validas.append({
                'min': round(b['min'], 4),
                'max': round(b['max'], 4),
                'tipo': b['tipo'],
                'eventos': n_eventos,
            })
    return bandas_validas


# ─── Conversor de formato — cascade -> scalp_engine (uso futuro/opcional) ──

def banda_para_formato_scalp(banda_cascade, ultimo_toque_ts=None):
    """
    Converte o formato de banda do cascade_engine ('min'/'max'/'tipo'/'eventos')
    pro formato que o scalp_engine espera ('top'/'bottom'/'toques'/'tipo_predominante').
    Se banda_cascade for None, retorna None (quem chamar cai no próprio
    cálculo interno, sem quebrar nada).
    """
    if not banda_cascade:
        return None
    tipo_predominante = 'demanda' if banda_cascade['tipo'] == 'suporte' else 'oferta'
    return {
        'top': banda_cascade['max'],
        'bottom': banda_cascade['min'],
        'toques': banda_cascade['eventos'],
        'ultimo_toque_ts': ultimo_toque_ts,
        'tipo_predominante': tipo_predominante,
    }


# ─── Estado próprio pra multi-TF (não mexe na tabela sr_zone_state) ───

def init_cascade_multi_tf_db(db_file):
    """Chamar junto com init_cascade_db() — tabela nova, aditiva."""
    with sqlite3.connect(db_file) as conn:
        cursor = conn.cursor()
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS cascade_multi_tf_signal_state (
                pair TEXT PRIMARY KEY,
                last_signature TEXT,
                updated_at INTEGER
            )
        ''')
        conn.commit()


def _ja_alertado_multi_tf(db_file, pair, signature):
    with sqlite3.connect(db_file) as conn:
        cursor = conn.cursor()
        cursor.execute('SELECT last_signature FROM cascade_multi_tf_signal_state WHERE pair=?', (pair,))
        row = cursor.fetchone()
        return bool(row and row[0] == signature)


def _salvar_alerta_multi_tf(db_file, pair, signature):
    with sqlite3.connect(db_file) as conn:
        cursor = conn.cursor()
        cursor.execute('''
            INSERT INTO cascade_multi_tf_signal_state (pair, last_signature, updated_at)
            VALUES (?, ?, ?)
            ON CONFLICT(pair) DO UPDATE SET last_signature=excluded.last_signature, updated_at=excluded.updated_at
        ''', (pair, signature, int(time.time())))
        conn.commit()


# ─── ORQUESTRADOR MULTI-TF — D1 + H4, confirmação no M15 ──────────────

def process_pair_full_multi_tf(db_file, pair, candles_d1, candles_h4, candles_m15,
                                 send_telegram_fn, fvg_ob_zona=None,
                                 proxima_zona_tecnica=None):
    """
    Roda a cascata em paralelo pra zona D1 E zona H4:
      zona (D1 ou H4) -> sweep M15 -> força -> CHoCH M15 -> RSI ->
      divergência -> score -> (se >=75) Sinal completo no Telegram,
      identificando de qual timeframe veio a zona.

    Se as duas (D1 e H4) confirmarem ao mesmo tempo, na mesma direção,
    marca como Confluência D1+H4 e soma +15 no score (score final
    limitado a 100).

    candles_d1 / candles_h4 / candles_m15: mesmo formato do
    fetch_bybit_klines do app.py, já buscados no loop de run_live_cycle
    (zero call extra à Bybit).
    """
    preco_atual = candles_m15[-1]['c']
    resultados = []

    fontes = [
        ('D1', calcular_bandas_sr_diario(candles_d1)),
        ('H4', calcular_bandas_sr_h4(candles_h4)),
    ]

    for tf_nome, bandas in fontes:
        banda_ativa, esta_dentro = banda_mais_relevante(bandas, preco_atual)
        if not esta_dentro or not banda_ativa:
            resultados.append({'tf': tf_nome, 'score': 0, 'motivo': f'preço fora de qualquer banda {tf_nome} válida'})
            continue

        sweep_ok, idx_sweep, direcao = detectar_sweep(candles_m15, banda_ativa)
        if not sweep_ok:
            resultados.append({
                'tf': tf_nome, 'score': 0, 'banda': banda_ativa,
                'motivo': f'na zona {tf_nome} ({banda_ativa["min"]}-{banda_ativa["max"]}), aguardando captura de liquidez (sweep M15)',
            })
            continue

        choch_ok, _ = detectar_choch(candles_m15, idx_sweep, direcao)
        if not choch_ok:
            resultados.append({
                'tf': tf_nome, 'score': 0, 'banda': banda_ativa, 'direcao': direcao,
                'motivo': f'sweep M15 ok (zona {tf_nome}), aguardando distribuição confirmada (CHoCH)',
            })
            continue

        forca_info = forca_do_sweep(candles_m15, idx_sweep)
        closes_m15 = [c['c'] for c in candles_m15]
        rsis = calcular_rsi(closes_m15)
        rsi_atual = rsis[-1]
        divergencia_ok = detectar_divergencia_rsi(candles_m15, rsis, direcao)

        score_info = calcular_score(
            banda_ativa, sweep_ok, choch_ok, forca_info,
            fvg_ob_presente=bool(fvg_ob_zona), rsi_atual=rsi_atual,
            divergencia_ok=divergencia_ok,
        )
        score_info['tf'] = tf_nome
        score_info['direcao'] = direcao
        score_info['banda'] = banda_ativa
        score_info['idx_sweep'] = idx_sweep
        resultados.append(score_info)

    # ── Confluência: D1 e H4 confirmaram juntos, na mesma direção ──
    confirmados = [r for r in resultados if r.get('score', 0) >= SCORE_THRESHOLD_SINAL]
    if len(confirmados) == 2 and confirmados[0].get('direcao') == confirmados[1].get('direcao'):
        for r in confirmados:
            r['score'] = min(r['score'] + 15, 100)
            r['confluencia_multi_tf'] = True
            r['detalhes'] = r.get('detalhes', []) + [('confluencia_d1_h4', 15)]

    for r in confirmados:
        forca_info_sl = forca_do_sweep(candles_m15, r.get('idx_sweep'))
        sltp = calcular_sl_tp(r['banda'], r['direcao'], forca_info_sl, fvg_ob_zona, proxima_zona_tecnica)

        tag = " (Confluência D1+H4 🔥)" if r.get('confluencia_multi_tf') else f" (Zona {r['tf']})"
        signature = f"{r['tf']}|{r['direcao']}|{round(preco_atual, 2)}|{r['score']}"

        if not _ja_alertado_multi_tf(db_file, pair, signature):
            msg = _formatar_msg_sinal(pair, r['direcao'], r, preco_atual, sltp['sl'], sltp['tp'])
            msg += f"\n<i>Origem: {r['tf']}{tag}</i>"
            send_telegram_fn(msg)
            _salvar_alerta_multi_tf(db_file, pair, signature)

        r['sl'] = sltp['sl']
        r['tp'] = sltp['tp']

    return resultados


# ═══════════════════════════════════════════════════════════════════════
# INTEGRAÇÃO NO app.py — 4 pontos, todos aditivos, nada é removido
# ═══════════════════════════════════════════════════════════════════════
"""
1) Topo do app.py, junto dos outros imports:

    import cascade_engine

2) Logo depois de `init_db()`:

    cascade_engine.init_cascade_db(DB_FILE)
    cascade_engine.init_cascade_signal_db(DB_FILE)
    cascade_engine.init_cascade_multi_tf_db(DB_FILE)

3) Dentro de run_live_cycle(), o loop que já busca os candles por TF:

    for tf_label, interval in LIVE_TF_INTERVALS.items():
        candles = fetch_bybit_klines(symbol, interval, 200)
        base64_png = render_live_chart_png_base64(candles, pair_label, tf_label)
        images_by_tf[tf_label] = {'base64': base64_png, 'mimeType': 'image/png'}

   Troca só por (guarda D1, H4 e M15 que já estão sendo buscados, sem
   nenhuma call nova à Bybit):

    candles_por_tf_cache = {}
    for tf_label, interval in LIVE_TF_INTERVALS.items():
        candles = fetch_bybit_klines(symbol, interval, 200)
        candles_por_tf_cache[tf_label] = candles
        base64_png = render_live_chart_png_base64(candles, pair_label, tf_label)
        images_by_tf[tf_label] = {'base64': base64_png, 'mimeType': 'image/png'}

4) Logo depois desse loop, ANTES de chamar analyze_single_pair (a
   chamada à Claude API continua acontecendo normalmente — isso aqui
   roda em paralelo, sem custo de IA):

    if 'D1' in candles_por_tf_cache and 'M15' in candles_por_tf_cache:
        try:
            cascade_engine.process_pair_full(
                DB_FILE, pair,
                candles_por_tf_cache['D1'],
                candles_por_tf_cache['M15'],
                send_telegram,
            )
        except Exception as e:
            print(f"[cascade_engine] erro no ciclo de {pair}: {e}")

    if 'D1' in candles_por_tf_cache and 'H4' in candles_por_tf_cache and 'M15' in candles_por_tf_cache:
        try:
            cascade_engine.process_pair_full_multi_tf(
                DB_FILE, pair,
                candles_por_tf_cache['D1'],
                candles_por_tf_cache['H4'],
                candles_por_tf_cache['M15'],
                send_telegram,
            )
        except Exception as e:
            print(f"[cascade_engine] erro no ciclo multi-tf de {pair}: {e}")

O try/except é de propósito: se a cascata der qualquer erro, ela só
loga e segue — o resto do ciclo (Trade Ao Vivo, Claude API, journal,
Telegram do score >=75 que já existe) continua rodando normalmente,
sem interrupção nenhuma.
"""
