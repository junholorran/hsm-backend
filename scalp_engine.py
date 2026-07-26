# scalp_engine.py
# ─────────────────────────────────────────────────────────────────────────
# Motor de Scalp Ao Vivo — aditivo, não mexe em nada do cascade_engine.
#
# Fluxo (nesta ordem, igual foi validado com o Juninho):
#   1. Zona D1 (cluster de S/R, banda — não linha fina)
#   2. Killzone (London 07h-10h ou NY 13h-16h, horário Portugal) — INFORMATIVO.
#      Cripto não fecha e tem volume real fora dessas janelas também, então
#      a killzone NÃO bloqueia mais sinal nenhum — só soma um bônus de
#      qualidade no score quando o sweep/CHoCH acontece dentro dela, e
#      aparece no texto pra dar contexto.
#   3. Sweep — preço varre liquidez na borda da zona D1. Uma vez detectado,
#      fica GRAVADO (não se perde se o candle sair da janela recente),
#      só é esquecido se a zona D1 mudar de verdade ou passar muito tempo
#      (12h) sem confirmar CHoCH.
#   4. CHoCH — no timeframe de EXECUÇÃO escolhido pelo utilizador
#      (M1/M5/M15), confirma virada de estrutura depois do sweep
#   5. Retorno à FVG/OB deixado pelo CHoCH — zona de entrada
#   6. Score determinístico (mesmo espírito do cascade: soma de pesos,
#      nunca "sensação"). Sinaliza/alerta Telegram com score >= 75,
#      independente de estar ou não na killzone.
#
# Tudo isolado em tabelas próprias (scalp_zone_state, scalp_signal_state),
# nunca toca nas tabelas do cascade_engine ou do app.py.
#
# ── MODO "REJEIÇÃO ANTECIPADA v2" (aditivo, seção no final do arquivo) ──
# Segundo modo, mais agressivo, que roda em PARALELO ao modo acima. Não
# espera CHoCH confirmar — dispara quando um pavio varre LIQUIDEZ ANTIGA
# real (fundo/topo estabelecido) na borda da zona D1 E o RSI está extremo
# (<=20 ou >=80) naquele candle. Regra fechada (tudo ou nada, não é score
# gradual): zona + liquidez antiga + RSI extremo, os 3 juntos, ou nada.
# Divergência de RSI (Camada 16 do prompt principal) é BÔNUS de confiança,
# não obrigatória — soma ao texto/score da mensagem, não bloqueia sinal.
# ─────────────────────────────────────────────────────────────────────────

import sqlite3
import time
import random
from datetime import datetime, timezone, timedelta

SCORE_THRESHOLD_SINAL = 75
COOLDOWN_SECONDS = 45 * 60  # 45min — meio-termo da faixa 30-60min do Vortex
TOLERANCIA_CLUSTER_PCT = 0.006   # 0.6% — mesma tolerância usada no cascade pra clusterizar toques
MIN_EVENTOS_BANDA = 2            # mínimo de toques pra uma banda D1 ser considerada válida
SWING_LOOKBACK = 5               # candles de cada lado pra confirmar swing high/low no TF de execução
SWEEP_MEMORY_MAX_AGE_SECONDS = 12 * 3600  # sweep salvo expira depois de 12h sem confirmar CHoCH

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
        # ── NOVO: tabela separada pro modo antecipado, não mistura
        # com scalp_signal_state do modo normal. ──
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS scalp_antecipado_signal_state (
                id TEXT PRIMARY KEY,
                pair TEXT,
                created_at INTEGER,
                exec_tf TEXT,
                direcao TEXT,
                rsi REAL,
                liquidez_varrida REAL,
                entry REAL,
                sl REAL,
                tp REAL,
                alerted INTEGER DEFAULT 0
            )
        ''')
        conn.commit()
        # ── MODO SOMBRA-like: coluna nova pra registar se houve divergência
        # de RSI confirmada no sinal antecipado. Aditivo via ALTER TABLE,
        # idempotente (ignora erro se já existir). ──
        try:
            cursor.execute("ALTER TABLE scalp_antecipado_signal_state ADD COLUMN divergencia_rsi INTEGER DEFAULT 0")
            conn.commit()
        except Exception:
            pass


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
            swings.append({'index': i, 'tipo': 'high', 'valor': c['h'], 't': c['t']})
        if all(c['l'] <= o['l'] for o in window if o is not c):
            swings.append({'index': i, 'tipo': 'low', 'valor': c['l'], 't': c['t']})
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
    (CHoCH real, confirmado por fechamento, não só pavio).

    Usa TIMESTAMP do sweep (sweep['t']), não a posição/index dele — isso
    é essencial pra funcionar mesmo quando o sweep foi detectado em um
    ciclo anterior e o candle original já rolou pra fora da janela atual
    de exec_candles (o par ainda é o mesmo par, só a janela de candles
    "andou" pra frente no tempo)."""
    swings = detect_exec_swings(exec_candles)
    ref = None
    for s in swings:
        if s['t'] <= sweep['t']:
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

    for i, c in enumerate(exec_candles):
        if c['t'] <= ref['t']:
            continue
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


def find_ifvg_after_choch(exec_candles, choch):
    """iFVG (Inversion Fair Value Gap): se o FVG formado pelo movimento do
    CHoCH for ROMPIDO de vez (preço fecha totalmente do outro lado dele,
    não só toca) e depois volta e REJEITA naquele mesmo nível, o gap
    inverte de papel — vira zona de entrada com sentido invertido do que
    era antes, mas SEMPRE na mesma direção do CHoCH (funciona tanto pra
    suporte quanto resistência, dependendo de qual lado o preço vem).
    É um fallback do FVG/OB normal — só entra em ação se aquele não
    servir mais (rompido de vez)."""
    start = max(0, choch['index'] - 1)
    end = min(len(exec_candles) - 1, choch['index'] + 4)

    for i in range(start + 1, end):
        if i + 1 >= len(exec_candles):
            break
        prev, nxt = exec_candles[i - 1], exec_candles[i + 1]
        gap_top = gap_bottom = None
        if choch['direcao'] == 'alta' and nxt['l'] > prev['h']:
            gap_top, gap_bottom = nxt['l'], prev['h']
        elif choch['direcao'] == 'baixa' and nxt['h'] < prev['l']:
            gap_top, gap_bottom = prev['l'], nxt['h']
        if gap_top is None:
            continue

        # procura fechamento TOTAL do outro lado do gap (rompimento de
        # verdade, não só um pavio tocando) — é isso que inverte o FVG
        violado_idx = None
        for k in range(i + 2, len(exec_candles)):
            c = exec_candles[k]
            if choch['direcao'] == 'alta' and c['c'] < gap_bottom:
                violado_idx = k
                break
            if choch['direcao'] == 'baixa' and c['c'] > gap_top:
                violado_idx = k
                break
        if violado_idx is None:
            continue

        # depois do rompimento, procura o preço voltar a tocar o gap e
        # REJEITAR na direção original do CHoCH — isso confirma o iFVG
        for k in range(violado_idx + 1, len(exec_candles)):
            c = exec_candles[k]
            tocou = c['l'] <= gap_top and c['h'] >= gap_bottom
            if not tocou:
                continue
            rejeitou = c['c'] > gap_top if choch['direcao'] == 'alta' else c['c'] < gap_bottom
            if rejeitou:
                return {'tipo': 'iFVG', 'top': gap_top, 'bottom': gap_bottom}
            break
    return None


def find_breaker_block_after_choch(exec_candles, choch):
    """Breaker Block: um Order Block que foi ROMPIDO de vez pelo próprio
    movimento do CHoCH e depois o preço volta e RESPEITA aquele nível na
    direção do CHoCH — sequência ICT clássica de 3 passos: (1) OB
    original (cor oposta à direção do CHoCH), (2) rompimento total desse
    OB (fechamento do outro lado, não só pavio), (3) retorno respeitando
    o nível na nova direção. Funciona tanto pra suporte quanto
    resistência, dependendo de que lado o preço vem. Fallback final —
    só entra em ação depois de FVG/iFVG/OB simples não servirem."""
    start = max(0, choch['index'] - 12)
    for i in range(choch['index'] - 1, start, -1):
        c = exec_candles[i]
        up = c['c'] >= c['o']
        if choch['direcao'] == 'alta' and up:
            continue
        if choch['direcao'] == 'baixa' and not up:
            continue
        ob_top = c['o'] if choch['direcao'] == 'alta' else c['c']
        ob_bottom = c['c'] if choch['direcao'] == 'alta' else c['o']
        if ob_top <= ob_bottom:
            continue

        # esse OB precisa ter sido rompido DE VEZ (fechamento, não pavio)
        # por um candle entre ele e o CHoCH
        violado_idx = None
        for k in range(i + 1, choch['index'] + 1):
            cc = exec_candles[k]
            if choch['direcao'] == 'alta' and cc['c'] < ob_bottom:
                violado_idx = k
                break
            if choch['direcao'] == 'baixa' and cc['c'] > ob_top:
                violado_idx = k
                break
        if violado_idx is None:
            continue

        # e depois disso, o preço precisa voltar e REJEITAR na direção
        # do CHoCH — isso confirma o breaker block
        for k in range(violado_idx + 1, len(exec_candles)):
            cc = exec_candles[k]
            tocou = cc['l'] <= ob_top and cc['h'] >= ob_bottom
            if not tocou:
                continue
            rejeitou = cc['c'] > ob_top if choch['direcao'] == 'alta' else cc['c'] < ob_bottom
            if rejeitou:
                return {'tipo': 'Breaker', 'top': ob_top, 'bottom': ob_bottom}
            break
    return None


def price_in_zone(entry_zone, preco):
    return entry_zone['bottom'] <= preco <= entry_zone['top']


def _load_saved_state(db_file, pair):
    """Lê o último estado salvo desse par (zona/sweep/CHoCH), pra decidir
    se dá pra reaproveitar um sweep já detectado em ciclo anterior."""
    try:
        with sqlite3.connect(db_file) as conn:
            cursor = conn.cursor()
            cursor.execute('''
                SELECT zona_top, zona_bottom, sweep_ts, sweep_nivel, sweep_lado, updated_at
                FROM scalp_zone_state WHERE pair=?
            ''', (pair,))
            row = cursor.fetchone()
        if not row:
            return None
        return {
            'zona_top': row[0], 'zona_bottom': row[1],
            'sweep_ts': row[2], 'sweep_nivel': row[3], 'sweep_lado': row[4],
            'updated_at': row[5],
        }
    except Exception as e:
        print(f"[scalp_engine] erro ao carregar estado salvo de {pair}: {e}")
        return None


def _sweep_ainda_valido(saved, zona, now):
    """Um sweep salvo continua valendo se: a zona D1 não mudou de forma
    relevante desde que foi salvo, e não passou tempo demais sem CHoCH
    confirmar. Isso é o que evita o sweep 'sumir' só porque o candle
    saiu da janela recente de exec_candles."""
    if not saved or saved.get('sweep_nivel') is None or not saved.get('sweep_lado'):
        return False
    if saved.get('zona_top') is None or saved.get('zona_bottom') is None:
        return False
    largura = zona['top'] - zona['bottom']
    if largura <= 0:
        return False
    if abs(saved['zona_top'] - zona['top']) > largura or abs(saved['zona_bottom'] - zona['bottom']) > largura:
        return False
    idade = now - (saved.get('updated_at') or 0)
    if idade > SWEEP_MEMORY_MAX_AGE_SECONDS:
        return False
    return True


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


# ═══════════════════════════════════════════════════════════════════════
# INDICADORES TÉCNICOS PADRÃO — cálculo real (fórmulas de mercado
# convencionais), não texto gerado. Pedido explícito do Juninho depois de
# ver a lista completa que o Vortex usa (RSI, MACD, ATR, ADX, Bollinger,
# EMAs, Stochastic). Cada função devolve uma SÉRIE (lista, mesmo tamanho
# dos candles, com None nos pontos sem dado suficiente ainda) — quem
# consome pega o último valor válido com a função `last()` no fundo do
# arquivo (compute_technical_indicators).
# ═══════════════════════════════════════════════════════════════════════

def compute_ema(values, period):
    """Média móvel exponencial padrão. Primeiro valor válido é uma SMA
    simples do período (ponto de partida clássico), depois aplica o fator
    de suavização k = 2/(period+1)."""
    n = len(values)
    if n < period:
        return [None] * n
    ema = [None] * n
    k = 2 / (period + 1)
    sma_inicial = sum(values[:period]) / period
    ema[period - 1] = sma_inicial
    for i in range(period, n):
        ema[i] = values[i] * k + ema[i - 1] * (1 - k)
    return ema


def compute_macd(closes, fast=12, slow=26, signal_period=9):
    """MACD padrão: linha = EMA rápida - EMA lenta; sinal = EMA da linha;
    histograma = linha - sinal. Alinha os índices manualmente porque a
    EMA lenta (26) começa bem depois da rápida (12)."""
    n = len(closes)
    ema_fast = compute_ema(closes, fast)
    ema_slow = compute_ema(closes, slow)

    macd_line = [None] * n
    for i in range(n):
        if ema_fast[i] is not None and ema_slow[i] is not None:
            macd_line[i] = ema_fast[i] - ema_slow[i]

    valid_idx = [i for i, v in enumerate(macd_line) if v is not None]
    signal_line = [None] * n
    if valid_idx:
        macd_values = [macd_line[i] for i in valid_idx]
        ema_sinal_sub = compute_ema(macd_values, signal_period)
        for j, idx in enumerate(valid_idx):
            signal_line[idx] = ema_sinal_sub[j]

    histogram = [None] * n
    for i in range(n):
        if macd_line[i] is not None and signal_line[i] is not None:
            histogram[i] = macd_line[i] - signal_line[i]

    return macd_line, signal_line, histogram


def compute_atr(candles, period=14):
    """Average True Range, suavização de Wilder (padrão do mercado —
    mesma usada no Supertrend, que também depende de ATR)."""
    n = len(candles)
    if n < period + 1:
        return [None] * n
    tr = [None] * n
    for i in range(1, n):
        h, l, prev_c = candles[i]['h'], candles[i]['l'], candles[i - 1]['c']
        tr[i] = max(h - l, abs(h - prev_c), abs(l - prev_c))

    atr = [None] * n
    primeiros_tr = [tr[i] for i in range(1, period + 1)]
    atr[period] = sum(primeiros_tr) / period
    for i in range(period + 1, n):
        atr[i] = (atr[i - 1] * (period - 1) + tr[i]) / period
    return atr


def compute_adx(candles, period=14):
    """ADX de Wilder — força de tendência (não direção). +DI/-DI internos
    calculados por suavização de Wilder, DX = diferença normalizada entre
    eles, ADX = média suavizada do DX."""
    n = len(candles)
    if n < period * 2 + 2:
        return [None] * n

    plus_dm = [0.0] * n
    minus_dm = [0.0] * n
    tr = [0.0] * n
    for i in range(1, n):
        up_move = candles[i]['h'] - candles[i - 1]['h']
        down_move = candles[i - 1]['l'] - candles[i]['l']
        plus_dm[i] = up_move if (up_move > down_move and up_move > 0) else 0.0
        minus_dm[i] = down_move if (down_move > up_move and down_move > 0) else 0.0
        h, l, prev_c = candles[i]['h'], candles[i]['l'], candles[i - 1]['c']
        tr[i] = max(h - l, abs(h - prev_c), abs(l - prev_c))

    atr_s = [None] * n
    plus_di_s = [None] * n
    minus_di_s = [None] * n
    dx = [None] * n

    atr_s[period] = sum(tr[1:period + 1])
    plus_di_s[period] = sum(plus_dm[1:period + 1])
    minus_di_s[period] = sum(minus_dm[1:period + 1])

    def _dx_de(plus_s, minus_s, atr_val):
        if not atr_val:
            return None
        pdi = 100 * plus_s / atr_val
        mdi = 100 * minus_s / atr_val
        if pdi + mdi == 0:
            return 0.0
        return 100 * abs(pdi - mdi) / (pdi + mdi)

    dx[period] = _dx_de(plus_di_s[period], minus_di_s[period], atr_s[period])

    for i in range(period + 1, n):
        atr_s[i] = atr_s[i - 1] - (atr_s[i - 1] / period) + tr[i]
        plus_di_s[i] = plus_di_s[i - 1] - (plus_di_s[i - 1] / period) + plus_dm[i]
        minus_di_s[i] = minus_di_s[i - 1] - (minus_di_s[i - 1] / period) + minus_dm[i]
        dx[i] = _dx_de(plus_di_s[i], minus_di_s[i], atr_s[i])

    adx = [None] * n
    janela_inicial = [v for v in dx[period:period * 2] if v is not None]
    if len(janela_inicial) < period:
        return adx
    idx_primeiro_adx = period * 2 - 1
    adx[idx_primeiro_adx] = sum(janela_inicial) / period
    for i in range(idx_primeiro_adx + 1, n):
        if dx[i] is None:
            continue
        adx[i] = (adx[i - 1] * (period - 1) + dx[i]) / period
    return adx


def compute_bollinger(closes, period=20, std_mult=2):
    """Bandas de Bollinger: SMA central + desvio-padrão populacional das
    últimas `period` velas, multiplicado por std_mult (padrão 2σ)."""
    n = len(closes)
    upper, mid, lower = [None] * n, [None] * n, [None] * n
    for i in range(period - 1, n):
        window = closes[i - period + 1:i + 1]
        m = sum(window) / period
        variancia = sum((x - m) ** 2 for x in window) / period
        desvio = variancia ** 0.5
        mid[i] = m
        upper[i] = m + std_mult * desvio
        lower[i] = m - std_mult * desvio
    return upper, mid, lower


def compute_stochastic(candles, k_period=14, d_period=3, smooth=3):
    """Estocástico lento (%K suavizado + %D): %K bruto = posição do close
    dentro do range high/low das últimas k_period velas, suavizado por
    `smooth` períodos; %D = média móvel de `d_period` sobre o %K já
    suavizado."""
    n = len(candles)
    raw_k = [None] * n
    for i in range(k_period - 1, n):
        window = candles[i - k_period + 1:i + 1]
        hh = max(c['h'] for c in window)
        ll = min(c['l'] for c in window)
        c_atual = candles[i]['c']
        raw_k[i] = 0.0 if hh == ll else 100 * (c_atual - ll) / (hh - ll)

    k = [None] * n
    for i in range(n):
        start = i - smooth + 1
        if start < 0 or raw_k[i] is None:
            continue
        window = raw_k[start:i + 1]
        if any(v is None for v in window):
            continue
        k[i] = sum(window) / smooth

    d = [None] * n
    for i in range(n):
        start = i - d_period + 1
        if start < 0 or k[i] is None:
            continue
        window = k[start:i + 1]
        if any(v is None for v in window):
            continue
        d[i] = sum(window) / d_period

    return k, d


def compute_vwap(exec_candles):
    """VWAP institucional, ancorado na sessão (dia UTC atual) — mesma
    lógica que mesas institucionais usam: preço médio ponderado por
    volume, resetado a cada novo dia. Usa apenas os candles do dia UTC
    corrente dentro da janela de exec_candles disponível."""
    if not exec_candles:
        return None
    dia_atual = datetime.fromtimestamp(exec_candles[-1]['t'], tz=timezone.utc).date()
    cum_pv, cum_vol = 0.0, 0.0
    for c in exec_candles:
        if datetime.fromtimestamp(c['t'], tz=timezone.utc).date() != dia_atual:
            continue
        typical = (c['h'] + c['l'] + c['c']) / 3
        vol = c.get('v', 0)
        cum_pv += typical * vol
        cum_vol += vol
    if cum_vol == 0:
        return None
    return round(cum_pv / cum_vol, 6)


def compute_volume_profile_poc(exec_candles, lookback=100, bins=24):
    """Volume Profile simplificado: agrupa os closes das últimas
    `lookback` velas em `bins` faixas de preço, soma o volume de cada
    faixa, e devolve o POC (Point of Control) — o preço onde mais volume
    trocou de mãos. Mesma ideia do POC pontilhado que aparece no chart
    do Vortex."""
    candles = exec_candles[-lookback:]
    if not candles:
        return None
    precos = [c['c'] for c in candles]
    lo, hi = min(precos), max(precos)
    if hi == lo:
        return round(lo, 6)
    largura_bin = (hi - lo) / bins
    vol_por_bin = [0.0] * bins
    for c in candles:
        idx = min(int((c['c'] - lo) / largura_bin), bins - 1)
        vol_por_bin[idx] += c.get('v', 0)
    idx_max = max(range(bins), key=lambda i: vol_por_bin[i])
    poc = lo + (idx_max + 0.5) * largura_bin
    return round(poc, 6)


def compute_ichimoku(exec_candles):
    """Ichimoku Kinko Hyo — Tenkan-sen (9), Kijun-sen (26), Senkou Span
    A e B (nuvem/Kumo). Valores calculados no ponto atual, sem o
    deslocamento de 26 períodos à frente que o Ichimoku tradicional usa
    pra desenhar a nuvem projetada — aqui reporta os valores DE HOJE
    (equivalente ao 'onde a nuvem está formando agora')."""
    def hh_ll(candles, period):
        window = candles[-period:]
        return max(c['h'] for c in window), min(c['l'] for c in window)

    if len(exec_candles) < 52:
        return {'tenkan': None, 'kijun': None, 'senkou_a': None, 'senkou_b': None}

    hh9, ll9 = hh_ll(exec_candles, 9)
    hh26, ll26 = hh_ll(exec_candles, 26)
    hh52, ll52 = hh_ll(exec_candles, 52)
    tenkan = (hh9 + ll9) / 2
    kijun = (hh26 + ll26) / 2
    senkou_a = (tenkan + kijun) / 2
    senkou_b = (hh52 + ll52) / 2
    return {
        'tenkan': round(tenkan, 6), 'kijun': round(kijun, 6),
        'senkou_a': round(senkou_a, 6), 'senkou_b': round(senkou_b, 6),
    }


def compute_monte_carlo(exec_candles, n_sims=1000, n_steps=20):
    """Simulação de Monte Carlo real: mede a média e o desvio-padrão dos
    retornos históricos recentes (não inventa volatilidade), e projeta
    `n_sims` caminhos aleatórios de `n_steps` candles à frente a partir
    do preço atual (passeio aleatório gaussiano com a volatilidade real
    medida). Devolve % de simulações que terminaram acima/abaixo do
    preço atual, e os cenários pessimista (percentil 10), mediano
    (percentil 50) e otimista (percentil 90) — mesmo espírito dos "1000
    sims" que o Vortex mostra."""
    closes = [c['c'] for c in exec_candles]
    if len(closes) < 30:
        return None
    retornos = [(closes[i] - closes[i - 1]) / closes[i - 1] for i in range(1, len(closes)) if closes[i - 1] > 0]
    if not retornos:
        return None
    media_r = sum(retornos) / len(retornos)
    var_r = sum((r - media_r) ** 2 for r in retornos) / len(retornos)
    desvio_r = var_r ** 0.5

    preco_atual = closes[-1]
    rng = random.Random()
    precos_finais = []
    for _ in range(n_sims):
        p = preco_atual
        for _ in range(n_steps):
            p = p * (1 + rng.gauss(media_r, desvio_r))
        precos_finais.append(p)

    precos_finais.sort()
    acima = sum(1 for p in precos_finais if p > preco_atual)
    p10 = precos_finais[int(n_sims * 0.10)]
    p50 = precos_finais[int(n_sims * 0.50)]
    p90 = precos_finais[int(n_sims * 0.90)]

    return {
        'prob_alta_pct': round(100 * acima / n_sims, 1),
        'prob_baixa_pct': round(100 * (n_sims - acima) / n_sims, 1),
        'cenario_pessimista': round(p10, 6),
        'cenario_mediano': round(p50, 6),
        'cenario_otimista': round(p90, 6),
        'n_sims': n_sims,
        'n_steps': n_steps,
    }


def detect_candle_pattern(exec_candles):
    """Detecta o padrão de candle mais recente, com regras geométricas
    reais (não classificação por 'achismo'): Doji, Engolfo de Alta/Baixa,
    Martelo, Estrela Cadente. Retorna None se o último candle não bater
    em nenhum padrão claro."""
    if len(exec_candles) < 2:
        return None
    c = exec_candles[-1]
    prev = exec_candles[-2]
    corpo = abs(c['c'] - c['o'])
    range_total = c['h'] - c['l']
    if range_total == 0:
        return None

    if corpo <= range_total * 0.1:
        return 'Doji'

    prev_bear = prev['c'] < prev['o']
    cur_bull = c['c'] > c['o']
    if prev_bear and cur_bull and c['c'] >= prev['o'] and c['o'] <= prev['c']:
        return 'Engolfo de Alta'

    prev_bull = prev['c'] > prev['o']
    cur_bear = c['c'] < c['o']
    if prev_bull and cur_bear and c['o'] >= prev['c'] and c['c'] <= prev['o']:
        return 'Engolfo de Baixa'

    pavio_inferior = min(c['o'], c['c']) - c['l']
    pavio_superior = c['h'] - max(c['o'], c['c'])
    if pavio_inferior >= corpo * 2 and pavio_superior <= corpo * 0.5:
        return 'Martelo (Hammer)'
    if pavio_superior >= corpo * 2 and pavio_inferior <= corpo * 0.5:
        return 'Estrela Cadente (Shooting Star)'

    return None


def compute_technical_indicators(exec_candles):
    """Roda todos os indicadores acima em cima do TF de execução e
    devolve só o ÚLTIMO valor válido de cada um — é isso que entra no
    resultado do ciclo (scalp_result['indicadores']), pra tela mostrar
    os números reais igual o Vortex mostra, sem inventar nada."""
    closes = [c['c'] for c in exec_candles]

    rsi_series = compute_rsi(closes)
    ema9 = compute_ema(closes, 9)
    ema21 = compute_ema(closes, 21)
    ema50 = compute_ema(closes, 50)
    ema200 = compute_ema(closes, 200)
    macd_line, macd_signal, macd_hist = compute_macd(closes)
    atr_series = compute_atr(exec_candles, 14)
    adx_series = compute_adx(exec_candles, 14)
    bb_upper, bb_mid, bb_lower = compute_bollinger(closes, 20, 2)
    stoch_k, stoch_d = compute_stochastic(exec_candles, 14, 3, 3)

    def last(series):
        for v in reversed(series):
            if v is not None:
                return round(v, 4)
        return None

    indicadores = {
        'rsi14': last(rsi_series),
        'ema9': last(ema9), 'ema21': last(ema21), 'ema50': last(ema50), 'ema200': last(ema200),
        'macd_line': last(macd_line), 'macd_signal': last(macd_signal), 'macd_hist': last(macd_hist),
        'atr14': last(atr_series),
        'adx14': last(adx_series),
        'bollinger_upper': last(bb_upper), 'bollinger_mid': last(bb_mid), 'bollinger_lower': last(bb_lower),
        'stoch_k': last(stoch_k), 'stoch_d': last(stoch_d),
    }

    # ── Bloco 2: VWAP, Volume Profile, Ichimoku, Monte Carlo, Candle
    # Patterns — cada um com try/except isolado, pra um indicador falhar
    # (ex: par muito novo, poucos candles) nunca derrubar os outros. ──
    try:
        indicadores['vwap'] = compute_vwap(exec_candles)
    except Exception as e:
        print(f"[scalp_engine] erro no VWAP: {e}")
        indicadores['vwap'] = None

    try:
        indicadores['volume_profile_poc'] = compute_volume_profile_poc(exec_candles)
    except Exception as e:
        print(f"[scalp_engine] erro no Volume Profile: {e}")
        indicadores['volume_profile_poc'] = None

    try:
        indicadores['ichimoku'] = compute_ichimoku(exec_candles)
    except Exception as e:
        print(f"[scalp_engine] erro no Ichimoku: {e}")
        indicadores['ichimoku'] = None

    try:
        indicadores['monte_carlo'] = compute_monte_carlo(exec_candles)
    except Exception as e:
        print(f"[scalp_engine] erro no Monte Carlo: {e}")
        indicadores['monte_carlo'] = None

    try:
        indicadores['candle_pattern'] = detect_candle_pattern(exec_candles)
    except Exception as e:
        print(f"[scalp_engine] erro no Candle Pattern: {e}")
        indicadores['candle_pattern'] = None

    return indicadores


def compute_score(zona, sweep, choch, entry_zone, exec_candles, na_killzone, indicadores=None):
    """Soma de pesos determinística, mesmo espírito do cascade_engine.
    Killzone aqui é só um BÔNUS de qualidade (cripto tem volume real 24h,
    não é gate) — soma pontos se bateu dentro da janela, mas a ausência
    dela não impede o score de chegar em 75.

    `indicadores` é o dict já calculado por compute_technical_indicators()
    (reaproveitado do início do ciclo, pra não recalcular tudo de novo).
    Se vier None (chamada antiga/isolada), calcula na hora como fallback.

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
    if entry_zone['tipo'] in ('FVG', 'iFVG'):
        pts_fvg_ob = 20
    elif entry_zone['tipo'] == 'Breaker':
        pts_fvg_ob = 18
    else:
        pts_fvg_ob = 15
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

    # ── NOVO: indicadores técnicos pesando de verdade no score, não só
    # aparecendo como dado solto. Reaproveita `indicadores` já calculado
    # no início do ciclo (compute_technical_indicators) — se não vier
    # (chamada isolada/antiga), calcula na hora como fallback. ──
    if indicadores is None:
        indicadores = compute_technical_indicators(exec_candles)
    direcao = choch['direcao']

    # MACD: histograma a favor da direção = momentum real confirmando
    macd_hist = indicadores.get('macd_hist')
    if macd_hist is not None:
        if (direcao == 'alta' and macd_hist > 0) or (direcao == 'baixa' and macd_hist < 0):
            score += 8
            detalhes.append(('macd_confirmando', 8))

    # ADX: força de tendência real (>=25 é padrão de mercado pra "tendência
    # de verdade", <20 é lateral/fraco) — não indica direção, só se vale a
    # pena confiar no movimento
    adx = indicadores.get('adx14')
    if adx is not None and adx >= 25:
        score += 7
        detalhes.append(('adx_tendencia_forte', 7))

    # EMAs empilhadas na direção certa (9>21>50 pra alta, invertido pra
    # baixa) = estrutura de tendência de curto prazo alinhada
    ema9, ema21, ema50 = indicadores.get('ema9'), indicadores.get('ema21'), indicadores.get('ema50')
    if ema9 is not None and ema21 is not None and ema50 is not None:
        if direcao == 'alta' and ema9 > ema21 > ema50:
            score += 8
            detalhes.append(('emas_alinhadas', 8))
        elif direcao == 'baixa' and ema9 < ema21 < ema50:
            score += 8
            detalhes.append(('emas_alinhadas', 8))

    # Stochastic: %K e %D ambos do lado favorável (mesmo espírito do RSI,
    # mas outro oscilador — reforça sem duplicar o mesmo sinal)
    stoch_k, stoch_d = indicadores.get('stoch_k'), indicadores.get('stoch_d')
    if stoch_k is not None and stoch_d is not None:
        if direcao == 'alta' and stoch_k <= 30 and stoch_d <= 30:
            score += 6
            detalhes.append(('stochastic_favoravel', 6))
        elif direcao == 'baixa' and stoch_k >= 70 and stoch_d >= 70:
            score += 6
            detalhes.append(('stochastic_favoravel', 6))

    # Bollinger: preço no candle de CHoCH tocou/passou a banda oposta à
    # direção (ex: CHoCH de alta com o sweep tendo tocado a banda inferior)
    # = movimento esticado o suficiente pra justificar reversão real, não
    # só ruído dentro do canal
    bb_lower, bb_upper = indicadores.get('bollinger_lower'), indicadores.get('bollinger_upper')
    if bb_lower is not None and bb_upper is not None and sweep.get('nivel') is not None:
        if direcao == 'alta' and sweep['nivel'] <= bb_lower:
            score += 5
            detalhes.append(('bollinger_extremo_favoravel', 5))
        elif direcao == 'baixa' and sweep['nivel'] >= bb_upper:
            score += 5
            detalhes.append(('bollinger_extremo_favoravel', 5))

    # ATR: valida se o risco (distância entrada→stop) é saudável em
    # relação à volatilidade real do par — stop grudado demais no preço
    # (menos de 0.5x ATR) é fácil de ser varrido por ruído; stop longe
    # demais (mais de 4x ATR) incha o risco sem necessidade. Faixa
    # saudável = confirma que o stop tá calibrado com o mercado real.
    atr = indicadores.get('atr14')
    if atr and atr > 0 and sweep.get('nivel') is not None:
        preco_atual_est = exec_candles[-1]['c']
        risco_est = abs(preco_atual_est - sweep['nivel'])
        razao_atr = risco_est / atr
        if 0.5 <= razao_atr <= 4:
            score += 5
            detalhes.append(('atr_risco_saudavel', 5))

    preco_atual = exec_candles[-1]['c']

    # VWAP institucional: preço do lado certo do VWAP (acima pra compra,
    # abaixo pra venda) = viés institucional da sessão alinhado
    vwap = indicadores.get('vwap')
    if vwap is not None:
        if direcao == 'alta' and preco_atual > vwap:
            score += 5
            detalhes.append(('vwap_alinhado', 5))
        elif direcao == 'baixa' and preco_atual < vwap:
            score += 5
            detalhes.append(('vwap_alinhado', 5))

    # Volume Profile POC: entrada perto do preço onde mais volume trocou
    # de mãos recentemente = zona com liquidez/interesse real, não vazio
    poc = indicadores.get('volume_profile_poc')
    if poc is not None and poc > 0:
        distancia_pct = abs(preco_atual - poc) / poc
        if distancia_pct <= 0.008:  # dentro de 0.8% do POC
            score += 5
            detalhes.append(('poc_proximo', 5))

    # Ichimoku: preço FORA da nuvem (Kumo) na direção certa = estrutura
    # de tendência mais ampla concordando, não só o TF de execução
    ichimoku = indicadores.get('ichimoku') or {}
    senkou_a, senkou_b = ichimoku.get('senkou_a'), ichimoku.get('senkou_b')
    if senkou_a is not None and senkou_b is not None:
        topo_nuvem = max(senkou_a, senkou_b)
        fundo_nuvem = min(senkou_a, senkou_b)
        if direcao == 'alta' and preco_atual > topo_nuvem:
            score += 6
            detalhes.append(('ichimoku_fora_nuvem', 6))
        elif direcao == 'baixa' and preco_atual < fundo_nuvem:
            score += 6
            detalhes.append(('ichimoku_fora_nuvem', 6))

    # Monte Carlo: maioria das 1000 simulações (baseadas na volatilidade
    # real medida) termina do lado da direção do sinal
    mc = indicadores.get('monte_carlo') or {}
    prob_alta = mc.get('prob_alta_pct')
    prob_baixa = mc.get('prob_baixa_pct')
    if prob_alta is not None and prob_baixa is not None:
        if direcao == 'alta' and prob_alta >= 55:
            score += 6
            detalhes.append(('monte_carlo_favoravel', 6))
        elif direcao == 'baixa' and prob_baixa >= 55:
            score += 6
            detalhes.append(('monte_carlo_favoravel', 6))

    # Candle Pattern: padrão de vela do último candle bate com a direção
    # do sinal (Engolfo/Martelo pra alta, Engolfo/Estrela Cadente pra baixa)
    padrao = indicadores.get('candle_pattern')
    padroes_alta = ('Engolfo de Alta', 'Martelo (Hammer)')
    padroes_baixa = ('Engolfo de Baixa', 'Estrela Cadente (Shooting Star)')
    if padrao:
        if direcao == 'alta' and padrao in padroes_alta:
            score += 6
            detalhes.append(('candle_pattern_favoravel', 6))
        elif direcao == 'baixa' and padrao in padroes_baixa:
            score += 6
            detalhes.append(('candle_pattern_favoravel', 6))

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
        'zona_ativa': False,
        'sweep_nivel': None,
        'sweep_lado': None,
        'choch_nivel': None,
        'choch_direcao': None,
        'entry_zone_top': None,
        'entry_zone_bottom': None,
        'entry_zone_tipo': None,
        'indicadores': None,
        'em_cooldown': False,
    }

    # ── Indicadores técnicos calculados SEMPRE, independente de ter zona/
    # sweep/CHoCH ou não — igual o Vortex mostra a lista de indicadores
    # já na tela desde o início do "ANALISANDO...", antes de qualquer
    # confluência ter sido encontrada. Envolvido em try/except pra nunca
    # derrubar o ciclo por causa de dado insuficiente (par novo, poucos
    # candles, etc.) — nesse caso só fica None. ──
    try:
        resultado['indicadores'] = compute_technical_indicators(exec_candles)
    except Exception as e:
        print(f"[scalp_engine] erro ao calcular indicadores de {pair}: {e}")

    if not zona:
        # ── preço saiu da zona, mas isso NÃO apaga a memória salva.
        # Antes disso, o ciclo salvava fase='idle' com zona/sweep=None,
        # o que zerava tudo no banco — perdia o sweep mesmo que a zona
        # continuasse válida. Agora só mostra a ÚLTIMA zona/sweep/CHoCH
        # conhecidos (marcados como não-ativos), e só deixa expirar pelo
        # mesmo critério de sempre (12h sem confirmar, ou zona mudou de
        # verdade — ver _sweep_ainda_valido). ──
        saved = _load_saved_state(db_file, pair)
        if saved and saved.get('zona_top') is not None:
            resultado['zona_top'] = round(saved['zona_top'], 6)
            resultado['zona_bottom'] = round(saved['zona_bottom'], 6)
            resultado['zona_ativa'] = False
            if saved.get('sweep_nivel') is not None:
                resultado['sweep_nivel'] = round(saved['sweep_nivel'], 6)
                resultado['sweep_lado'] = saved['sweep_lado']
            resultado['motivo'] = 'preço fora da zona D1 (última zona mapeada mantida)'
        else:
            resultado['motivo'] = 'preço fora de qualquer banda D1 válida'
        return resultado

    resultado['zona_top'] = round(zona['top'], 6)
    resultado['zona_bottom'] = round(zona['bottom'], 6)
    resultado['zona_ativa'] = True

    # ── Sweep: tenta achar um NOVO nos candles recentes; se não achar,
    # reaproveita o sweep salvo do ciclo anterior (se ainda válido). Se
    # achar um novo mais recente que o salvo, usa o novo. Isso é o que
    # evita o sweep "sumir" só porque o candle original saiu da janela. ──
    fresh_sweep = detect_sweep_in_zone(exec_candles, zona)
    saved = _load_saved_state(db_file, pair)
    saved_valido = _sweep_ainda_valido(saved, zona, now)

    sweep = None
    if fresh_sweep and (not saved_valido or fresh_sweep['t'] >= saved['sweep_ts']):
        sweep = fresh_sweep
    elif saved_valido:
        sweep = {'t': saved['sweep_ts'], 'nivel': saved['sweep_nivel'], 'lado': saved['sweep_lado']}

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
        entry_zone = find_ifvg_after_choch(exec_candles, choch)
    if not entry_zone:
        entry_zone = find_breaker_block_after_choch(exec_candles, choch)
    if not entry_zone:
        resultado['motivo'] = 'CHoCH confirmado, mas sem FVG/OB/iFVG/Breaker de retorno ainda'
        _save_zone_state(db_file, pair, zona, 'choch', now, sweep=sweep, choch=choch)
        return resultado

    resultado['entry_zone_top'] = round(entry_zone['top'], 6)
    resultado['entry_zone_bottom'] = round(entry_zone['bottom'], 6)
    resultado['entry_zone_tipo'] = entry_zone['tipo']

    if not price_in_zone(entry_zone, preco_atual):
        resultado['motivo'] = 'preço ainda fora da zona de entrada (FVG/OB/iFVG/Breaker) — aguardando retorno'
        _save_zone_state(db_file, pair, zona, 'aguardando_retorno', now, sweep=sweep, choch=choch)
        return resultado

    score, detalhes = compute_score(zona, sweep, choch, entry_zone, exec_candles, na_killzone, indicadores=resultado.get('indicadores'))
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
        # ── Filtro de Cooldown: mesmo com score válido, não reenvia
        # Telegram se já alertou esse par há menos de 45min. Evita spam
        # quando o score fica alto por vários ciclos seguidos (a mesma
        # zona/sweep/CHoCH ainda em vigor). O resultado continua sendo
        # devolvido normalmente pra tela do app — só o Telegram é
        # segurado. ──
        segundos_desde = _segundos_desde_ultimo_alerta(db_file, 'scalp_signal_state', pair)
        em_cooldown = segundos_desde is not None and segundos_desde < COOLDOWN_SECONDS
        resultado['em_cooldown'] = em_cooldown

        if em_cooldown:
            restante_min = (COOLDOWN_SECONDS - segundos_desde) // 60
            resultado['motivo'] = f'score {score} válido, mas em cooldown ({restante_min}min restantes)'
            _save_zone_state(db_file, pair, zona, 'cooldown', now, sweep=sweep, choch=choch)
            _save_signal(db_file, pair, exec_tf_label, resultado, alerted=False)
        else:
            resultado['motivo'] = 'entrada'
            _save_zone_state(db_file, pair, zona, 'entrada', now, sweep=sweep, choch=choch)
            _save_signal(db_file, pair, exec_tf_label, resultado, alerted=True)
            if send_telegram_fn:
                arrow = '📈' if choch['direcao'] == 'alta' else '📉'
                kz_txt = f" | Killzone: {killzone_nome}" if na_killzone else " | fora da killzone"
                msg = f"⚡ <b>Sinal Scalp Ao Vivo — {pair}</b>\n\n"
                msg += f"{arrow} <b>{'LONG' if choch['direcao']=='alta' else 'SHORT'}</b> | TF execução: {exec_tf_label}{kz_txt}\n"
                msg += f"📍 Entrada: {resultado['entry']}\n"
                msg += f"🛑 Stop: {resultado['sl']}\n"
                msg += f"✅ TP: {resultado['tp']}\n"
                msg += f"🎯 Score: {score}/100\n"
                msg += f"\n💡 Zona D1 → Sweep → CHoCH → retorno {entry_zone['tipo']}"
                send_telegram_fn(msg)
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


def _segundos_desde_ultimo_alerta(db_file, table, pair):
    """Lê o timestamp do último sinal REALMENTE alertado (alerted=1) pra
    esse par nessa tabela, e devolve quantos segundos se passaram desde
    então. None se nunca alertou. Usado pelo filtro de Cooldown, pra
    evitar spam de Telegram quando a condição continua batendo ciclo
    após ciclo (ex: score fica em 78 por 3 ciclos seguidos)."""
    try:
        with sqlite3.connect(db_file) as conn:
            cursor = conn.cursor()
            cursor.execute(
                f'SELECT created_at FROM {table} WHERE pair=? AND alerted=1 ORDER BY created_at DESC LIMIT 1',
                (pair,)
            )
            row = cursor.fetchone()
        if not row:
            return None
        return int(time.time()) - row[0]
    except Exception as e:
        print(f"[scalp_engine] erro ao checar cooldown ({table}, {pair}): {e}")
        return None


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


# ═══════════════════════════════════════════════════════════════════════
# MODO "REJEIÇÃO ANTECIPADA v2" — sem CHoCH, baseado em pavio varrendo
# liquidez antiga real + RSI extremo. Regra fechada (tudo ou nada).
# Roda em PARALELO ao process_pair_scalp() normal, chamado à parte no
# run_live_cycle() do app.py, reaproveitando os mesmos candles.
# ═══════════════════════════════════════════════════════════════════════

RSI_EXTREMO_BAIXA = 20   # RSI <= 20 no fundo = venda capitulando de vez
RSI_EXTREMO_ALTA = 80    # RSI >= 80 no topo = compra esticada de vez
LIQUIDEZ_LOOKBACK = 40   # candles pra trás pra achar o fundo/topo antigo que foi varrido
RR_FIXO_ANTECIPADO = 2.0  # TP = 2x o risco, sempre
ANTECIPADO_SWEEP_LOOKBACK = 10  # candles recentes onde procurar o pavio de sweep


def find_liquidez_antiga(exec_candles, ate_index, tipo, lookback=LIQUIDEZ_LOOKBACK):
    """
    Procura, ANTES do candle de rejeição (ate_index), um fundo (tipo='low')
    ou topo (tipo='high') estabelecido — não o candle vizinho, um nível
    que já tinha sido tocado antes e ficou como referência de liquidez.

    Retorna (valor, index) desse nível antigo, ou (None, None) se não
    achar nada claro. O índice é devolvido agora (antes só o valor) pra
    dar pra comparar o RSI daquele candle antigo com o RSI do candle de
    sweep atual — é isso que permite checar divergência de verdade,
    e não só "RSI tá baixo agora".
    """
    inicio = max(0, ate_index - lookback)
    janela = exec_candles[inicio:ate_index - 2]  # exclui os 2 candles mais próximos do sweep
    if len(janela) < 5:
        return None, None

    if tipo == 'low':
        idx_relativo = min(range(len(janela)), key=lambda k: janela[k]['l'])
        valor = janela[idx_relativo]['l']
    else:
        idx_relativo = max(range(len(janela)), key=lambda k: janela[k]['h'])
        valor = janela[idx_relativo]['h']

    idx_absoluto = inicio + idx_relativo
    return valor, idx_absoluto


def detect_sweep_liquidez_antiga(exec_candles, zona, lookback=ANTECIPADO_SWEEP_LOOKBACK):
    """
    Versão mais rigorosa do pavio: só conta se o candle varreu (pavio
    ultrapassou) um nível de liquidez ANTIGA real — não só a borda da
    zona D1. Precisa: pavio rompe o nível antigo, MAS fecha de volta
    para dentro dele (rejeição confirmada).

    Retorna dict {'index', 'lado', 'nivel_pavio' (mínima/máxima exata do
    candle, pro stop), 'liquidez_varrida', 'liquidez_index'} ou None.
    """
    recentes_idx = range(max(0, len(exec_candles) - lookback), len(exec_candles))

    for i in recentes_idx:
        c = exec_candles[i]

        # rejeição no topo (short) — varreu um topo antigo E fechou abaixo dele
        if c['h'] >= zona['top']:
            liq_antiga, liq_index = find_liquidez_antiga(exec_candles, i, 'high')
            if liq_antiga and c['h'] > liq_antiga and c['c'] < liq_antiga:
                return {
                    'index': i, 'lado': 'baixa',
                    'nivel_pavio': c['h'],  # stop vai ACIMA disso
                    'liquidez_varrida': liq_antiga,
                    'liquidez_index': liq_index,
                    't': c['t'],
                }

        # rejeição no fundo (long) — varreu um fundo antigo E fechou acima dele
        if c['l'] <= zona['bottom']:
            liq_antiga, liq_index = find_liquidez_antiga(exec_candles, i, 'low')
            if liq_antiga and c['l'] < liq_antiga and c['c'] > liq_antiga:
                return {
                    'index': i, 'lado': 'alta',
                    'nivel_pavio': c['l'],  # stop vai ABAIXO disso
                    'liquidez_varrida': liq_antiga,
                    'liquidez_index': liq_index,
                    't': c['t'],
                }

    return None


def compute_bias_from_swings(candles, lookback=SWING_LOOKBACK):
    """
    Bias estrutural de um timeframe qualquer (D1, H4, etc.), reaproveitando
    a mesma lógica de swing high/low já usada no TF de execução — nada de
    indicador novo, é a leitura de estrutura ICT padrão:
      - últimos 2 topos SUBINDO e últimos 2 fundos SUBINDO -> 'alta'
      - últimos 2 topos DESCENDO e últimos 2 fundos DESCENDO -> 'baixa'
      - qualquer outra combinação (topos e fundos não concordam, ou não
        há swings suficientes ainda) -> 'neutro', o padrão seguro que
        NÃO bloqueia sinal nenhum.
    """
    if not candles or len(candles) < (lookback * 2 + 5):
        return 'neutro'
    swings = detect_exec_swings(candles, lookback=lookback)
    highs = [s for s in swings if s['tipo'] == 'high'][-2:]
    lows = [s for s in swings if s['tipo'] == 'low'][-2:]
    if len(highs) == 2 and len(lows) == 2:
        topos_sobem = highs[1]['valor'] > highs[0]['valor']
        fundos_sobem = lows[1]['valor'] > lows[0]['valor']
        topos_descem = highs[1]['valor'] < highs[0]['valor']
        fundos_descem = lows[1]['valor'] < lows[0]['valor']
        if topos_sobem and fundos_sobem:
            return 'alta'
        if topos_descem and fundos_descem:
            return 'baixa'
    return 'neutro'


def rsi_extremo_no_candle(exec_candles, idx):
    """RSI no exato candle do sweep."""
    closes = [c['c'] for c in exec_candles[:idx + 1]]
    if len(closes) < 15:
        return None
    rsi_series = compute_rsi(closes)
    return rsi_series[-1]


def rsi_no_candle(exec_candles, idx):
    """RSI em qualquer candle específico (não só o mais recente) — usado
    pra comparar o RSI do sweep atual com o RSI do fundo/topo antigo,
    na checagem de divergência."""
    if idx is None or idx < 0:
        return None
    closes = [c['c'] for c in exec_candles[:idx + 1]]
    if len(closes) < 15:
        return None
    rsi_series = compute_rsi(closes)
    return rsi_series[-1]


def check_rsi_divergence(exec_candles, sweep):
    """
    Divergência de RSI real (mesma regra rígida da Camada 16 do prompt
    principal do Claude): compara o candle do sweep atual com o candle
    da liquidez antiga (fundo/topo) que foi varrida.

    Bullish (sweep['lado']=='alta', varreu fundo antigo pra baixo):
      preço faz fundo IGUAL ou MAIS BAIXO que o antigo, mas RSI do sweep
      atual é MAIOR que o RSI do fundo antigo -> divergência de alta.

    Bearish (sweep['lado']=='baixa', varreu topo antigo pra cima):
      preço faz topo IGUAL ou MAIS ALTO que o antigo, mas RSI do sweep
      atual é MENOR que o RSI do topo antigo -> divergência de baixa.

    Retorna True/False. Se não der pra calcular (RSI None de algum lado),
    retorna False — sem divergência confirmada é o padrão seguro, nunca
    assume a favor sem dado real.
    """
    liq_index = sweep.get('liquidez_index')
    if liq_index is None:
        return False

    rsi_liquidez_antiga = rsi_no_candle(exec_candles, liq_index)
    rsi_sweep_atual = rsi_no_candle(exec_candles, sweep['index'])
    if rsi_liquidez_antiga is None or rsi_sweep_atual is None:
        return False

    if sweep['lado'] == 'alta':
        # varreu fundo antigo -> preço igual/mais baixo, RSI mais alto = divergência de alta
        preco_igual_ou_mais_baixo = sweep['nivel_pavio'] <= sweep['liquidez_varrida']
        rsi_mais_alto = rsi_sweep_atual > rsi_liquidez_antiga
        return preco_igual_ou_mais_baixo and rsi_mais_alto

    if sweep['lado'] == 'baixa':
        # varreu topo antigo -> preço igual/mais alto, RSI mais baixo = divergência de baixa
        preco_igual_ou_mais_alto = sweep['nivel_pavio'] >= sweep['liquidez_varrida']
        rsi_mais_baixo = rsi_sweep_atual < rsi_liquidez_antiga
        return preco_igual_ou_mais_alto and rsi_mais_baixo

    return False


def _save_antecipado_signal(db_file, pair, exec_tf_label, resultado, alerted):
    try:
        signal_id = f"antecip_{pair}_{int(time.time()*1000)}"
        with sqlite3.connect(db_file) as conn:
            cursor = conn.cursor()
            cursor.execute('''
                INSERT INTO scalp_antecipado_signal_state
                    (id, pair, created_at, exec_tf, direcao, rsi, liquidez_varrida, entry, sl, tp, alerted, divergencia_rsi)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ''', (
                signal_id, pair, int(time.time()), exec_tf_label,
                resultado['direcao'], resultado['rsi'], resultado['liquidez_varrida'],
                resultado['entry'], resultado['sl'], resultado['tp'], 1 if alerted else 0,
                1 if resultado.get('divergencia_rsi') else 0,
            ))
            conn.commit()
    except Exception as e:
        print(f"[scalp_engine] erro ao salvar signal antecipado de {pair}: {e}")


def process_pair_scalp_antecipado_v2(db_file, pair, d1_candles, exec_candles, exec_tf_label,
                                      send_telegram_fn=None, h4_candles=None):
    """
    Regra fechada (tudo ou nada, não é score gradual) pras 4 condições
    OBRIGATÓRIAS:
    1. Zona D1 (suporte ou resistência)
    2. Pavio varre liquidez ANTIGA real (não só a borda da zona)
    3. RSI extremo no candle do sweep (<=20 ou >=80)
    4. Timeframes maiores (D1 e H4, quando disponível) não estão em
       estrutura clara CONTRA a direção do sinal — evita comprar suporte
       no meio de uma tendência de baixa forte no D1/H4 (ou vender
       resistência numa tendência de alta forte). 'neutro' passa sempre;
       só bloqueia se o bias maior estiver realmente na direção oposta.
    Se as 4 baterem juntas: Entrada = preço atual, Stop = mínima/máxima
    exata do pavio, TP = RR 2:1. Sem as 4 juntas, não há sinal.

    h4_candles é opcional — se não vier (chamada antiga sem esse
    parâmetro), a checagem de alinhamento usa só o D1 e não quebra nada
    que já estava funcionando.

    Divergência de RSI (comparando o sweep atual com o fundo/topo antigo
    varrido) é BÔNUS — não é obrigatória pra disparar o sinal, mas quando
    presente é reportada no resultado e destacada na mensagem do Telegram
    como reforço extra de confiança, igual pedido: "se tiver divergência,
    melhor ainda".
    """
    preco_atual = exec_candles[-1]['c']
    bandas = compute_d1_zones(d1_candles)
    zona = find_active_zone(bandas, preco_atual)

    resultado = {
        'pair': pair, 'exec_tf': exec_tf_label, 'modo': 'antecipado_v2',
        'sinal': False, 'direcao': None, 'entry': None, 'sl': None, 'tp': None,
        'rsi': None, 'liquidez_varrida': None, 'divergencia_rsi': False,
        'bias_d1': None, 'bias_h4': None, 'motivo': None, 'indicadores': None,
        'em_cooldown': False,
    }

    try:
        resultado['indicadores'] = compute_technical_indicators(exec_candles)
    except Exception as e:
        print(f"[scalp_engine] erro ao calcular indicadores (antecipado) de {pair}: {e}")

    if not zona:
        resultado['motivo'] = 'preço fora de qualquer banda D1 válida'
        return resultado

    sweep = detect_sweep_liquidez_antiga(exec_candles, zona)
    if not sweep:
        resultado['motivo'] = 'sem sweep de liquidez antiga confirmado'
        return resultado

    rsi_val = rsi_extremo_no_candle(exec_candles, sweep['index'])
    if rsi_val is None:
        resultado['motivo'] = 'RSI indisponível'
        return resultado

    direcao = sweep['lado']
    rsi_ok = (direcao == 'baixa' and rsi_val >= RSI_EXTREMO_ALTA) or \
             (direcao == 'alta' and rsi_val <= RSI_EXTREMO_BAIXA)

    if not rsi_ok:
        resultado['motivo'] = f"sweep ok mas RSI não extremo (RSI={round(rsi_val,1)})"
        resultado['rsi'] = round(rsi_val, 1)
        return resultado

    # ── 4ª condição obrigatória: alinhamento com D1/H4 ──
    bias_d1 = compute_bias_from_swings(d1_candles)
    bias_h4 = compute_bias_from_swings(h4_candles) if h4_candles else 'neutro'
    resultado['bias_d1'] = bias_d1
    resultado['bias_h4'] = bias_h4

    contra_alta = direcao == 'alta' and (bias_d1 == 'baixa' or bias_h4 == 'baixa')
    contra_baixa = direcao == 'baixa' and (bias_d1 == 'alta' or bias_h4 == 'alta')
    if contra_alta or contra_baixa:
        resultado['rsi'] = round(rsi_val, 1)
        resultado['motivo'] = (
            f"sweep+RSI ok, mas timeframes maiores contra a direção "
            f"(D1={bias_d1}, H4={bias_h4}) — sinal descartado"
        )
        return resultado

    # ── as 4 condições obrigatórias bateram — checa divergência como
    # bônus (não bloqueia, só reforça) ──
    tem_divergencia = check_rsi_divergence(exec_candles, sweep)

    # todas as condições obrigatórias bateram — monta a entrada
    entry = preco_atual
    sl = sweep['nivel_pavio']
    risco = abs(entry - sl)
    tp = entry - risco * RR_FIXO_ANTECIPADO if direcao == 'baixa' else entry + risco * RR_FIXO_ANTECIPADO

    resultado.update({
        'sinal': True,
        'direcao': direcao,
        'entry': round(entry, 6),
        'sl': round(sl, 6),
        'tp': round(tp, 6),
        'rsi': round(rsi_val, 1),
        'liquidez_varrida': round(sweep['liquidez_varrida'], 6),
        'divergencia_rsi': tem_divergencia,
        'motivo': 'entrada_confirmada',
    })

    # dedup por preço (já existia): não repete o mesmo sinal (mesma
    # direção+entry quase idêntico) seguido.
    ja_alertado_preco = False
    try:
        with sqlite3.connect(db_file) as conn:
            cursor = conn.cursor()
            cursor.execute('''
                SELECT direcao, entry FROM scalp_antecipado_signal_state
                WHERE pair=? ORDER BY created_at DESC LIMIT 1
            ''', (pair,))
            row = cursor.fetchone()
            if row and row[0] == direcao and abs((row[1] or 0) - entry) / entry < 0.001:
                ja_alertado_preco = True
    except Exception:
        pass

    # ── NOVO: Cooldown por tempo (45min), igual ao modo normal — cobre
    # o caso de preço oscilar um pouco (passa no teste de "preço quase
    # idêntico") mas ainda ser essencialmente o mesmo sinal repetindo. ──
    segundos_desde = _segundos_desde_ultimo_alerta(db_file, 'scalp_antecipado_signal_state', pair)
    em_cooldown = segundos_desde is not None and segundos_desde < COOLDOWN_SECONDS
    resultado['em_cooldown'] = em_cooldown

    bloqueado = ja_alertado_preco or em_cooldown
    _save_antecipado_signal(db_file, pair, exec_tf_label, resultado, alerted=not bloqueado)

    if send_telegram_fn and not bloqueado:
        arrow = '📈' if direcao == 'alta' else '📉'
        label = 'LONG' if direcao == 'alta' else 'SHORT'
        msg = f"⚠️ <b>Rejeição de Liquidez Antiga — {pair}</b>\n\n"
        msg += f"{arrow} <b>{label}</b> | TF execução: {exec_tf_label} | RSI extremo: {resultado['rsi']}\n"
        msg += f"📊 Alinhamento: D1={bias_d1} | H4={bias_h4}\n"
        if tem_divergencia:
            msg += "🔺 <b>Divergência de RSI confirmada</b> — reforço extra de confiança\n"
        msg += f"💧 Liquidez antiga varrida: {resultado['liquidez_varrida']}\n"
        msg += f"📍 Entrada: {resultado['entry']}\n🛑 Stop: {resultado['sl']}\n✅ TP (RR 2:1): {resultado['tp']}\n\n"
        msg += "<b>Sem CHoCH confirmado — entrada agressiva, posição menor recomendada.</b>"
        send_telegram_fn(msg)
    elif em_cooldown and not ja_alertado_preco:
        restante_min = (COOLDOWN_SECONDS - segundos_desde) // 60
        resultado['motivo'] = f'entrada_confirmada, mas em cooldown ({restante_min}min restantes)'

    return resultado
