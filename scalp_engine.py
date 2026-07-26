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
from datetime import datetime, timezone, timedelta

SCORE_THRESHOLD_SINAL = 75
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


def compute_score(zona, sweep, choch, entry_zone, exec_candles, na_killzone):
    """Soma de pesos determinística, mesmo espírito do cascade_engine.
    Killzone aqui é só um BÔNUS de qualidade (cripto tem volume real 24h,
    não é gate) — soma pontos se bateu dentro da janela, mas a ausência
    dela não impede o score de chegar em 75.
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
    }

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
        'bias_d1': None, 'bias_h4': None, 'motivo': None,
    }

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

    # dedup simples: não repete o mesmo sinal (mesma direção+entry) seguido
    ja_alertado = False
    try:
        with sqlite3.connect(db_file) as conn:
            cursor = conn.cursor()
            cursor.execute('''
                SELECT direcao, entry FROM scalp_antecipado_signal_state
                WHERE pair=? ORDER BY created_at DESC LIMIT 1
            ''', (pair,))
            row = cursor.fetchone()
            if row and row[0] == direcao and abs((row[1] or 0) - entry) / entry < 0.001:
                ja_alertado = True
    except Exception:
        pass

    _save_antecipado_signal(db_file, pair, exec_tf_label, resultado, alerted=not ja_alertado)

    if send_telegram_fn and not ja_alertado:
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

    return resultado
