# scalp_engine.py
# ─────────────────────────────────────────────────────────────────────────
# Motor de Scalp Ao Vivo — aditivo, não mexe em nada do cascade_engine.
# ─────────────────────────────────────────────────────────────────────────

import sqlite3
import time
import random
import requests
import json
from flask import Blueprint, jsonify, current_app
from datetime import datetime, timezone, timedelta
from zoneinfo import ZoneInfo

SCORE_THRESHOLD_SINAL = 75
COOLDOWN_SECONDS = 45 * 60
TOLERANCIA_CLUSTER_PCT = 0.006
MIN_EVENTOS_BANDA = 2
MIN_FVG_GAP_PCT = 0.0005
MIN_CANDLE_BODY_RATIO = 0.35
STOP_BUFFER_PCT = 0.001
D1_LOOKBACK_DIAS = 200
ZONA_FORTE_TOLERANCIA_PCT = 0.0015
ZONA_FORTE_MIN_TOQUES = 3
SCALP_RAPIDO_COOLDOWN_SECONDS = 5 * 60
ZONA_MOVEL_LOOKBACK = 20
ZONA_MOVEL_MAX_LARGURA_PCT = 0.01
SWING_LOOKBACK = 5
SWEEP_MEMORY_MAX_AGE_SECONDS = 12 * 3600

RSI_GATE_BLOQUEIA_LONG_ACIMA = 60
RSI_GATE_BLOQUEIA_SHORT_ABAIXO = 40

REGIME_ADX_THRESHOLD = 20
REGIME_GATE_ATIVO = True

MIN_RR_GATE = 1.5
RR_GATE_ATIVO = True

RR_TARGET_NORMAL = 2.5
RR_TARGET_CONTINUACAO = 2.5
RR_TARGET_RAPIDO = 2.0
RR_TARGET_CASCATA = 3.0

MONTE_CARLO_GATE_MIN_PROB = 55
MONTE_CARLO_GATE_ATIVO = True

MODOS_ATIVOS = {
    '4camadas': True,                # réplica intencional da Vortex — mesma lógica de entrada, sem trava
    'gates_vortex': True,            # motor restrito — 8 passos + 6 gates, com trava de contradição
}


def compute_market_regime(candles, adx_threshold=REGIME_ADX_THRESHOLD):
    adx_series = compute_adx(candles, 14)
    adx_atual = next((v for v in reversed(adx_series) if v is not None), None)
    if adx_atual is None:
        return 'indefinido', None
    regime = 'trending' if adx_atual >= adx_threshold else 'ranging'
    return regime, round(adx_atual, 2)




NOMES_PADRAO_CANDLE_PT = {
    'Engolfo de Alta': 'Engolfo (Engulfing) — domínio comprador com momentum forte',
    'Engolfo de Baixa': 'Engolfo (Engulfing) — domínio vendedor com momentum forte',
    'Martelo (Hammer)': 'Martelo — rejeição de fundo com pavio longo',
    'Estrela Cadente (Shooting Star)': 'Estrela Cadente — rejeição de topo com pavio longo',
    'Doji': 'Doji — indecisão, sem domínio claro',
}


def _formatar_motivos_principais(regime, adx_val, evento_tipo, evento_direcao, entry_zone_tipo,
                                  score, gates, candle_pattern=None, na_killzone=False, killzone_nome=None):
    direcao_label = 'BULLISH' if evento_direcao == 'alta' else 'BEARISH'
    linhas = [
        f"• Regime: {regime.upper()}{f' (ADX {adx_val})' if adx_val is not None else ''}",
        f"• Estrutura: {evento_tipo}",
        f"• Gatilho: {entry_zone_tipo}" + (f" + {NOMES_PADRAO_CANDLE_PT.get(candle_pattern, candle_pattern)}" if candle_pattern else ""),
        f"• Direção: {direcao_label}",
        f"• Score: {score}/100",
    ]
    if na_killzone:
        linhas.append(f"• Killzone: {killzone_nome}")
    gates_txt = ', '.join(f"{g['nome'].replace('GATE_', '').replace('_', ' ')} ✅" for g in gates if g['passou'])
    if gates_txt:
        linhas.append(f"• Gates aprovados: {gates_txt}")
    return "\n".join(linhas)


def classificar_qualidade_rr(rr):
    if rr is None:
        return None
    if rr < 1.5:
        return f"BAIXA — Relação de 1:{rr} exige taxa de acerto alta (>{round(100/(1+rr))}%) pra ser lucrativo no longo prazo."
    elif rr < 2.5:
        return f"MODERADA — Relação de 1:{rr} é equilibrada, taxa de acerto de ~{round(100/(1+rr))}% já cobre o breakeven."
    else:
        return f"ALTA — Relação de 1:{rr} permite ser lucrativo mesmo com taxa de acerto abaixo de {round(100/(1+rr))}%."


CAPITAL_USUARIO_USD = None

PERFIS_RISCO = {
    'conservador': 0.0075,
    'moderado': 0.015,
    'agressivo': 0.025,
}


def calcular_position_sizing(capital, entry, sl, perfil='moderado'):
    if not capital or not entry or not sl or entry == sl:
        return None
    risco_pct = PERFIS_RISCO.get(perfil, PERFIS_RISCO['moderado'])
    valor_risco_usd = round(capital * risco_pct, 2)
    distancia_stop = abs(entry - sl)
    quantidade_sugerida = round(valor_risco_usd / distancia_stop, 6) if distancia_stop > 0 else None
    return {
        'perfil': perfil,
        'risco_pct': round(risco_pct * 100, 2),
        'valor_em_risco_usd': valor_risco_usd,
        'quantidade_sugerida': quantidade_sugerida,
    }


def classificar_forca_swing(swing_nivel, swing_tipo, candles, tolerancia_pct=0.001):
    if swing_nivel is None or not candles:
        return None
    for c in candles:
        if swing_tipo == 'high' and c['h'] > swing_nivel * (1 + tolerancia_pct):
            return 'weak'
        if swing_tipo == 'low' and c['l'] < swing_nivel * (1 - tolerancia_pct):
            return 'weak'
    return 'strong'


HORARIOS_RUINS_UTC = [
    {'nome': 'Transição Ásia-Europa', 'inicio_h': 5, 'fim_h': 7},
    {'nome': 'Fechamento de NY', 'inicio_h': 21, 'fim_h': 23},
]


def esta_em_horario_ruim():
    import datetime
    hora_utc = datetime.datetime.utcnow().hour
    for janela in HORARIOS_RUINS_UTC:
        if janela['inicio_h'] <= hora_utc < janela['fim_h']:
            return True, janela['nome']
    return False, None


def compute_sazonalidade_mensal(db_file, pair, meses_historico=24):
    import datetime
    tabelas = [
        'scalp_signal_state', 'scalp_signal_state_continuacao',
        'scalp_rapido_signal_state', 'scalp_cascata_signal_state',
        'scalp_antecipado_signal_state', 'scalp_indicadores_signal_state',
    ]
    mes_atual = datetime.datetime.utcnow().month
    cutoff = int(time.time()) - meses_historico * 30 * 86400
    wins, losses = 0, 0
    try:
        with sqlite3.connect(db_file) as conn:
            cursor = conn.cursor()
            for tabela in tabelas:
                try:
                    cursor.execute(
                        f"SELECT created_at, resultado_final FROM {tabela} "
                        f"WHERE pair=? AND alerted=1 AND created_at >= ? "
                        f"AND resultado_final IN ('win','loss')",
                        (pair, cutoff)
                    )
                    for created_at, resultado in cursor.fetchall():
                        mes_do_sinal = datetime.datetime.utcfromtimestamp(created_at).month
                        if mes_do_sinal == mes_atual:
                            if resultado == 'win':
                                wins += 1
                            else:
                                losses += 1
                except Exception:
                    continue
    except Exception as e:
        print(f"[scalp_engine] erro ao calcular sazonalidade de {pair}: {e}")
        return None

    total = wins + losses
    if total == 0:
        return {'mes': mes_atual, 'amostras': 0, 'motivo': 'sem histórico suficiente ainda nesse mês'}
    return {
        'mes': mes_atual,
        'amostras': total,
        'win_rate_pct': round(100 * wins / total, 1),
        'wins': wins,
        'losses': losses,
    }


_FEAR_GREED_CACHE = {'valor': None, 'classificacao': None, 'timestamp': 0}
FEAR_GREED_CACHE_TTL = 3600


def get_fear_greed_index():
    agora = time.time()
    if _FEAR_GREED_CACHE['valor'] is not None and (agora - _FEAR_GREED_CACHE['timestamp']) < FEAR_GREED_CACHE_TTL:
        return {'valor': _FEAR_GREED_CACHE['valor'], 'classificacao': _FEAR_GREED_CACHE['classificacao']}

    try:
        resp = requests.get('https://api.alternative.me/fng/?limit=1', timeout=5)
        resp.raise_for_status()
        data = resp.json()
        item = data['data'][0]
        valor = int(item['value'])
        classificacao_raw = item['value_classification']
        traducao = {
            'Extreme Fear': 'Medo Extremo', 'Fear': 'Medo', 'Neutral': 'Neutro',
            'Greed': 'Ganância', 'Extreme Greed': 'Ganância Extrema',
        }
        classificacao = traducao.get(classificacao_raw, classificacao_raw)
        _FEAR_GREED_CACHE.update({'valor': valor, 'classificacao': classificacao, 'timestamp': agora})
        return {'valor': valor, 'classificacao': classificacao}
    except Exception as e:
        print(f"[scalp_engine] erro ao buscar Fear & Greed Index: {e}")
        return None


def montar_bloco_analise_extra(db_file, pair, direcao, entry, sl, tp, tabela_para_sazonalidade,
                                sweep_nivel=None, sweep_tipo=None, exec_candles=None,
                                entry_zone_tipo=None, obs_com_mitigacao=None):
    linhas = []

    if entry and sl and tp:
        risco = abs(entry - sl)
        retorno = abs(tp - entry)
        if risco > 0:
            rr = round(retorno / risco, 2)
            qualidade = classificar_qualidade_rr(rr)
            if qualidade:
                linhas.append(f"📐 {qualidade}")

    if sweep_nivel is not None and sweep_tipo is not None and exec_candles:
        swing_tipo_liquidez = 'high' if sweep_tipo == 'alta' else 'low'
        forca = classificar_forca_swing(sweep_nivel, swing_tipo_liquidez, exec_candles)
        if forca:
            linhas.append(f"💧 Liquidez varrida: {'Strong (intacta até agora)' if forca=='strong' else 'Weak (já tinha sido testada antes)'}")

    if entry_zone_tipo and 'OB' in entry_zone_tipo and obs_com_mitigacao:
        ob_correspondente = next(
            (ob for ob in obs_com_mitigacao if ob.get('bottom') is not None and ob.get('top') is not None
             and ob['bottom'] <= (entry or 0) <= ob['top']), None
        )
        if ob_correspondente is not None:
            linhas.append(f"⚠️ Order Block {'já mitigado antes' if ob_correspondente['mitigado'] else 'ainda intacto (primeira vez)'}")

    try:
        saz = compute_sazonalidade_mensal(db_file, pair)
        if saz and saz.get('amostras', 0) > 0:
            linhas.append(f"📅 Sazonalidade real ({pair}, esse mês): {saz['win_rate_pct']}% de acerto em {saz['amostras']} sinais resolvidos")
    except Exception:
        pass

    try:
        fg = get_fear_greed_index()
        if fg:
            linhas.append(f"🌡️ Fear & Greed Index: {fg['valor']}/100 ({fg['classificacao']})")
    except Exception:
        pass

    if CAPITAL_USUARIO_USD and entry and sl:
        try:
            sizing = calcular_position_sizing(CAPITAL_USUARIO_USD, entry, sl, perfil='moderado')
            if sizing:
                linhas.append(
                    f"💼 Sizing sugerido (moderado, {sizing['risco_pct']}% de ${CAPITAL_USUARIO_USD}): "
                    f"risco de ${sizing['valor_em_risco_usd']} nesse trade"
                )
        except Exception:
            pass

    return "\n".join(linhas)


PT_UTC_OFFSET_HOURS = 1

KILLZONES = [
    {'nome': 'Ásia', 'inicio_h': 0, 'fim_h': 3},
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
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS scalp_indicadores_signal_state (
                id TEXT PRIMARY KEY,
                pair TEXT,
                created_at INTEGER,
                exec_tf TEXT,
                direcao TEXT,
                score INTEGER,
                votos_favor INTEGER,
                votos_total INTEGER,
                entry REAL,
                sl REAL,
                tp REAL,
                alerted INTEGER DEFAULT 0
            )
        ''')
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS scalp_zone_state_continuacao (
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
            CREATE TABLE IF NOT EXISTS scalp_signal_state_continuacao (
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
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS scalp_filtros_shadow (
                id TEXT PRIMARY KEY,
                pair TEXT,
                created_at INTEGER,
                exec_tf TEXT,
                direcao TEXT,
                score INTEGER,
                entry REAL,
                sl REAL,
                tp REAL,
                filtros_que_bloqueariam TEXT,
                resultado TEXT DEFAULT 'pendente'
            )
        ''')
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS scalp_rapido_signal_state (
                id TEXT PRIMARY KEY,
                pair TEXT,
                created_at INTEGER,
                exec_tf TEXT,
                direcao TEXT,
                entry REAL,
                sl REAL,
                tp REAL,
                zona_tipo TEXT,
                alerted INTEGER DEFAULT 0
            )
        ''')
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS scalp_cascata_signal_state (
                id TEXT PRIMARY KEY,
                pair TEXT,
                created_at INTEGER,
                exec_tf TEXT,
                direcao TEXT,
                entry REAL,
                sl REAL,
                tp REAL,
                bias_semanal TEXT,
                bias_d1 TEXT,
                bias_h4 TEXT,
                bias_h1 TEXT,
                evento_tipo TEXT,
                alerted INTEGER DEFAULT 0
            )
        ''')
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS scalp_cascata_zone_state (
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
        conn.commit()
        try:
            cursor.execute("ALTER TABLE scalp_antecipado_signal_state ADD COLUMN divergencia_rsi INTEGER DEFAULT 0")
            conn.commit()
        except Exception:
            pass
        try:
            cursor.execute("ALTER TABLE scalp_filtros_shadow ADD COLUMN resultado TEXT DEFAULT 'pendente'")
            conn.commit()
        except Exception:
            pass
        tabelas_com_gestao = [
            'scalp_signal_state', 'scalp_signal_state_continuacao',
            'scalp_rapido_signal_state', 'scalp_cascata_signal_state',
            'scalp_antecipado_signal_state', 'scalp_indicadores_signal_state',
        ]
        for tabela in tabelas_com_gestao:
            for alter_sql in [
                f"ALTER TABLE {tabela} ADD COLUMN be_movido INTEGER DEFAULT 0",
                f"ALTER TABLE {tabela} ADD COLUMN parcial_feita INTEGER DEFAULT 0",
                f"ALTER TABLE {tabela} ADD COLUMN status_gestao TEXT DEFAULT ''",
                f"ALTER TABLE {tabela} ADD COLUMN resultado_final TEXT DEFAULT 'pendente'",
            ]:
                try:
                    cursor.execute(alter_sql)
                    conn.commit()
                except Exception:
                    pass

        tabelas_com_motivo = [
            'scalp_signal_state', 'scalp_signal_state_continuacao', 'scalp_indicadores_signal_state',
        ]
        for tabela in tabelas_com_motivo:
            try:
                cursor.execute(f"ALTER TABLE {tabela} ADD COLUMN motivo_score TEXT DEFAULT ''")
                conn.commit()
            except Exception:
                pass


def is_in_killzone(now_utc=None):
    now_utc = now_utc or datetime.now(timezone.utc)
    pt_hour = (now_utc + timedelta(hours=PT_UTC_OFFSET_HOURS)).hour
    for kz in KILLZONES:
        if kz['inicio_h'] <= pt_hour < kz['fim_h']:
            return True, kz['nome']
    return False, None


def _extrair_swings_lux_algo(candles, swing_size=50):
    n = len(candles)
    if n < swing_size + 5:
        return []

    legs = [0] * n
    current_leg = 0
    for i in range(swing_size, n):
        window = candles[i - swing_size + 1:i + 1]
        highest = max(c['h'] for c in window)
        lowest = min(c['l'] for c in window)
        high_back = candles[i - swing_size]['h']
        low_back = candles[i - swing_size]['l']
        if high_back > highest:
            current_leg = 0
        elif low_back < lowest:
            current_leg = 1
        legs[i] = current_leg

    swings = []
    for i in range(swing_size + 1, n):
        if legs[i] == legs[i - 1]:
            continue
        idx_pivot = i - swing_size
        if idx_pivot < 0:
            continue
        if legs[i] == 1:
            swings.append({'valor': candles[idx_pivot]['l'], 'tipo': 'low', 't': candles[idx_pivot]['t']})
        else:
            swings.append({'valor': candles[idx_pivot]['h'], 'tipo': 'high', 't': candles[idx_pivot]['t']})
    return swings


def find_d1_order_blocks(d1_candles, swing_size=50, atr_period=14, atr_mult=1.0, lookback_dias=D1_LOOKBACK_DIAS):
    n = len(d1_candles)
    if n < swing_size + atr_period + 5:
        return []

    atr_series = compute_atr(d1_candles, atr_period)

    legs = [0] * n
    current_leg = 0
    swing_high_level = None
    swing_low_level = None
    swing_high_crossed = False
    swing_low_crossed = False

    obs = []

    for i in range(swing_size, n):
        window = d1_candles[i - swing_size + 1:i + 1]
        highest = max(c['h'] for c in window)
        lowest = min(c['l'] for c in window)
        high_back = d1_candles[i - swing_size]['h']
        low_back = d1_candles[i - swing_size]['l']
        if high_back > highest:
            current_leg = 0
        elif low_back < lowest:
            current_leg = 1
        legs[i] = current_leg

        if i > swing_size and legs[i] != legs[i - 1]:
            idx_pivot = i - swing_size
            if idx_pivot >= 0:
                if legs[i] == 1:
                    swing_low_level = d1_candles[idx_pivot]['l']
                    swing_low_crossed = False
                else:
                    swing_high_level = d1_candles[idx_pivot]['h']
                    swing_high_crossed = False

        c = d1_candles[i]
        atr_val = atr_series[i]

        if swing_high_level is not None and not swing_high_crossed and c['c'] > swing_high_level:
            swing_high_crossed = True
            for k in range(i, max(0, i - swing_size), -1):
                cand = d1_candles[k]
                if cand['c'] < cand['o']:
                    rng = cand['h'] - cand['l']
                    if atr_val and rng >= atr_val * atr_mult:
                        obs.append({
                            'top': cand['h'], 'bottom': cand['l'],
                            'tipo': 'demanda', 't': cand['t'],
                        })
                    break

        if swing_low_level is not None and not swing_low_crossed and c['c'] < swing_low_level:
            swing_low_crossed = True
            for k in range(i, max(0, i - swing_size), -1):
                cand = d1_candles[k]
                if cand['c'] > cand['o']:
                    rng = cand['h'] - cand['l']
                    if atr_val and rng >= atr_val * atr_mult:
                        obs.append({
                            'top': cand['h'], 'bottom': cand['l'],
                            'tipo': 'oferta', 't': cand['t'],
                        })
                    break

    if lookback_dias and obs:
        cutoff_ms = d1_candles[-1]['t'] - lookback_dias * 24 * 3600 * 1000
        obs = [o for o in obs if o['t'] >= cutoff_ms]

    vistos = set()
    unicos = []
    for o in obs:
        chave = (o['t'], o['tipo'])
        if chave not in vistos:
            vistos.add(chave)
            unicos.append(o)

    return unicos


SR_CHANNEL_PIVOT_PERIOD = 10
SR_CHANNEL_MAX_WIDTH_PCT = 2
SR_CHANNEL_MIN_STRENGTH = 1
SR_CHANNEL_MAX_NUMBER = 6
SR_CHANNEL_LOOKBACK_PERIOD = 290
SR_CHANNEL_WIDTH_BASIS_BARS = 300


def compute_sr_channels(
    d1_candles,
    pivot_period=SR_CHANNEL_PIVOT_PERIOD,
    channel_width_pct=SR_CHANNEL_MAX_WIDTH_PCT,
    min_strength=SR_CHANNEL_MIN_STRENGTH,
    max_number_sr=SR_CHANNEL_MAX_NUMBER,
    loopback=SR_CHANNEL_LOOKBACK_PERIOD,
):
    n = len(d1_candles)
    if n < pivot_period * 2 + 1:
        return []

    highs = [c['h'] for c in d1_candles]
    lows = [c['l'] for c in d1_candles]
    closes = [c['c'] for c in d1_candles]
    last_idx = n - 1

    pivots_cronologico = []
    for i in range(pivot_period, n - pivot_period):
        janela_h = highs[i - pivot_period:i] + highs[i + 1:i + pivot_period + 1]
        if highs[i] > max(janela_h):
            pivots_cronologico.append({'idx': i, 'valor': highs[i], 'tipo': 'high'})
        janela_l = lows[i - pivot_period:i] + lows[i + 1:i + pivot_period + 1]
        if lows[i] < min(janela_l):
            pivots_cronologico.append({'idx': i, 'valor': lows[i], 'tipo': 'low'})

    pivots_cronologico.sort(key=lambda p: p['idx'])
    pivots = [p for p in reversed(pivots_cronologico) if (last_idx - p['idx']) <= loopback]

    if not pivots:
        return []

    pivotvals = [p['valor'] for p in pivots]
    m = len(pivotvals)

    janela_300 = d1_candles[-SR_CHANNEL_WIDTH_BASIS_BARS:] if n >= SR_CHANNEL_WIDTH_BASIS_BARS else d1_candles
    prdhighest = max(c['h'] for c in janela_300)
    prdlowest = min(c['l'] for c in janela_300)
    cwidth = (prdhighest - prdlowest) * channel_width_pct / 100.0
    if cwidth <= 0:
        return []

    candidatos = []
    for i in range(m):
        lo = pivotvals[i]
        hi = lo
        numpp = 0
        for y in range(m):
            cpp = pivotvals[y]
            wdth = (hi - cpp) if cpp <= hi else (cpp - lo)
            if wdth <= cwidth:
                if cpp <= hi:
                    lo = min(lo, cpp)
                else:
                    hi = max(hi, cpp)
                numpp += 20
        candidatos.append({'hi': hi, 'lo': lo, 'forca': numpp})

    start_idx = max(0, last_idx - loopback)
    for cand in candidatos:
        h_, l_ = cand['hi'], cand['lo']
        toques = 0
        for k in range(start_idx, last_idx + 1):
            hk, lk = highs[k], lows[k]
            if (l_ <= hk <= h_) or (l_ <= lk <= h_):
                toques += 1
        cand['forca'] += toques

    usados = [False] * len(candidatos)
    selecionados = []
    limite = min(10, max_number_sr)
    for _ in range(limite):
        melhor_idx = -1
        melhor_forca = -1
        for idx, cand in enumerate(candidatos):
            if usados[idx]:
                continue
            if cand['forca'] > melhor_forca and cand['forca'] >= min_strength * 20:
                melhor_forca = cand['forca']
                melhor_idx = idx
        if melhor_idx < 0:
            break
        escolhido = candidatos[melhor_idx]
        selecionados.append(escolhido)
        hh, ll = escolhido['hi'], escolhido['lo']
        for idx, cand in enumerate(candidatos):
            if usados[idx]:
                continue
            if (ll <= cand['hi'] <= hh) or (ll <= cand['lo'] <= hh):
                usados[idx] = True
        usados[melhor_idx] = True

    preco_atual = closes[-1]
    resultado = []
    for ch in selecionados:
        top, bottom = ch['hi'], ch['lo']
        if top > preco_atual and bottom > preco_atual:
            tipo_predominante = 'oferta'
        elif top < preco_atual and bottom < preco_atual:
            tipo_predominante = 'demanda'
        else:
            tipo_predominante = 'mista'
        resultado.append({
            'top': top,
            'bottom': bottom,
            'toques': ch['forca'],
            'ultimo_toque_ts': d1_candles[-1]['t'],
            'tipo_predominante': tipo_predominante,
        })

    resultado.sort(key=lambda c: c['toques'], reverse=True)
    return resultado


def compute_d1_zones(d1_candles, lookback_dias=None, swing_size=50):
    return compute_sr_channels(d1_candles)


def compute_d1_zones_swing_cluster(d1_candles, lookback_dias=D1_LOOKBACK_DIAS, swing_size=50):
    swings = _extrair_swings_lux_algo(d1_candles, swing_size=swing_size)

    if lookback_dias and swings:
        cutoff_ms = d1_candles[-1]['t'] - lookback_dias * 24 * 3600 * 1000
        swings = [s for s in swings if s['t'] >= cutoff_ms]

    grupos = []
    for s in swings:
        colocado = False
        for g in grupos:
            diff_pct = abs(s['valor'] - g['nivel']) / g['nivel']
            if diff_pct <= TOLERANCIA_CLUSTER_PCT:
                g['pontos'].append(s['valor'])
                g['nivel'] = sum(g['pontos']) / len(g['pontos'])
                g['timestamps'].append(s['t'])
                g['tipos'].append(s['tipo'])
                colocado = True
                break
        if not colocado:
            grupos.append({'nivel': s['valor'], 'pontos': [s['valor']], 'timestamps': [s['t']], 'tipos': [s['tipo']]})

    bandas = []
    for g in grupos:
        if len(g['pontos']) >= MIN_EVENTOS_BANDA:
            largura = g['nivel'] * TOLERANCIA_CLUSTER_PCT
            n_low = g['tipos'].count('low')
            n_high = g['tipos'].count('high')
            if n_low > n_high:
                tipo_predominante = 'demanda'
            elif n_high > n_low:
                tipo_predominante = 'oferta'
            else:
                tipo_predominante = 'mista'
            bandas.append({
                'top': g['nivel'] + largura,
                'bottom': g['nivel'] - largura,
                'toques': len(g['pontos']),
                'ultimo_toque_ts': max(g['timestamps']),
                'tipo_predominante': tipo_predominante,
            })
    return bandas


def find_active_zone(bandas, preco_atual):
    candidatas = [b for b in bandas if b['bottom'] <= preco_atual <= b['top']]
    if not candidatas:
        return None
    return max(candidatas, key=lambda b: b.get('ultimo_toque_ts', 0))


def compute_zona_diaria_movel(d1_candles):
    if len(d1_candles) < 2:
        return None
    candle_ontem = d1_candles[-2]
    corpo_top = max(candle_ontem['o'], candle_ontem['c'])
    corpo_bottom = min(candle_ontem['o'], candle_ontem['c'])
    return {
        'resistencia': {'top': candle_ontem['h'], 'bottom': corpo_top},
        'suporte': {'top': corpo_bottom, 'bottom': candle_ontem['l']},
        'candle_ts': candle_ontem['t'],
    }


def compute_zona_forte(d1_candles, tolerancia_pct=ZONA_FORTE_TOLERANCIA_PCT, min_toques=ZONA_FORTE_MIN_TOQUES, lookback_dias=D1_LOOKBACK_DIAS):
    if lookback_dias and len(d1_candles) > lookback_dias:
        d1_candles = d1_candles[-lookback_dias:]

    swings = []
    lb = 3
    for i in range(lb, len(d1_candles) - lb):
        c = d1_candles[i]
        is_high = all(c['h'] >= d1_candles[j]['h'] for j in range(i - lb, i + lb + 1) if j != i)
        is_low = all(c['l'] <= d1_candles[j]['l'] for j in range(i - lb, i + lb + 1) if j != i)
        if is_high:
            swings.append({'valor': c['h'], 'tipo': 'high', 't': c['t']})
        if is_low:
            swings.append({'valor': c['l'], 'tipo': 'low', 't': c['t']})

    grupos = []
    for s in swings:
        colocado = False
        for g in grupos:
            diff_pct = abs(s['valor'] - g['nivel']) / g['nivel']
            if diff_pct <= tolerancia_pct:
                g['pontos'].append(s['valor'])
                g['nivel'] = sum(g['pontos']) / len(g['pontos'])
                g['timestamps'].append(s['t'])
                colocado = True
                break
        if not colocado:
            grupos.append({'nivel': s['valor'], 'pontos': [s['valor']], 'timestamps': [s['t']]})

    zonas = []
    for g in grupos:
        if len(g['pontos']) >= min_toques:
            largura = g['nivel'] * tolerancia_pct
            zonas.append({
                'top': g['nivel'] + largura,
                'bottom': g['nivel'] - largura,
                'toques': len(g['pontos']),
                'ultimo_toque_ts': max(g['timestamps']),
            })
    return zonas


def compute_zona_movel(candles, lookback=ZONA_MOVEL_LOOKBACK):
    janela = candles[-lookback:] if len(candles) > lookback else candles
    if not janela:
        return None
    top = max(c['h'] for c in janela)
    bottom = min(c['l'] for c in janela)
    meio = (top + bottom) / 2
    largura_pct = (top - bottom) / meio if meio else 0
    return {
        'top': top,
        'bottom': bottom,
        'largura_pct': largura_pct,
        'ultimo_candle_ts': janela[-1]['t'],
    }


def compute_lux_structure_bias(candles, swing_size=50):
    n = len(candles)
    if n < swing_size + 5:
        return 'neutro'

    legs = [0] * n
    current_leg = 0
    for i in range(swing_size, n):
        window = candles[i - swing_size + 1:i + 1]
        highest = max(c['h'] for c in window)
        lowest = min(c['l'] for c in window)
        high_back = candles[i - swing_size]['h']
        low_back = candles[i - swing_size]['l']
        if high_back > highest:
            current_leg = 0
        elif low_back < lowest:
            current_leg = 1
        legs[i] = current_leg

    swing_high_level = None
    swing_low_level = None
    swing_high_crossed = False
    swing_low_crossed = False
    bias = 'neutro'

    for i in range(swing_size + 1, n):
        if legs[i] != legs[i - 1]:
            idx_pivot = i - swing_size
            if idx_pivot < 0:
                continue
            if legs[i] == 1:
                swing_low_level = candles[idx_pivot]['l']
                swing_low_crossed = False
            else:
                swing_high_level = candles[idx_pivot]['h']
                swing_high_crossed = False

        c = candles[i]
        if swing_high_level is not None and not swing_high_crossed and c['c'] > swing_high_level:
            bias = 'alta'
            swing_high_crossed = True
        if swing_low_level is not None and not swing_low_crossed and c['c'] < swing_low_level:
            bias = 'baixa'
            swing_low_crossed = True

    return bias


def compute_premium_discount(exec_candles, lookback=ZONA_MOVEL_LOOKBACK):
    donch = compute_zona_movel(exec_candles, lookback)
    if not donch:
        return None
    equilibrium = (donch['top'] + donch['bottom']) / 2
    return {'top': donch['top'], 'bottom': donch['bottom'], 'equilibrium': equilibrium}


def find_open_fvgs(exec_candles, lookback=100, min_gap_pct=MIN_FVG_GAP_PCT):
    candles = exec_candles[-lookback:] if len(exec_candles) > lookback else exec_candles
    abertas = []
    n = len(candles)
    for i in range(1, n - 1):
        prev, nxt = candles[i - 1], candles[i + 1]
        if nxt['l'] > prev['h']:
            gap_pct = (nxt['l'] - prev['h']) / prev['h'] if prev['h'] else 0
            if gap_pct >= min_gap_pct:
                top, bottom = nxt['l'], prev['h']
                preenchida = any(c['l'] <= bottom for c in candles[i + 2:])
                if not preenchida:
                    abertas.append({
                        'tipo': 'FVG_bullish', 'top': round(top, 6), 'bottom': round(bottom, 6),
                        't': candles[i]['t'], 'gap_pct': round(gap_pct * 100, 4),
                    })
        if nxt['h'] < prev['l']:
            gap_pct = (prev['l'] - nxt['h']) / prev['l'] if prev['l'] else 0
            if gap_pct >= min_gap_pct:
                top, bottom = prev['l'], nxt['h']
                preenchida = any(c['h'] >= top for c in candles[i + 2:])
                if not preenchida:
                    abertas.append({
                        'tipo': 'FVG_bearish', 'top': round(top, 6), 'bottom': round(bottom, 6),
                        't': candles[i]['t'], 'gap_pct': round(gap_pct * 100, 4),
                    })
    return abertas


def find_order_blocks(exec_candles, lookback=100):
    candles = exec_candles[-lookback:] if len(exec_candles) > lookback else exec_candles
    obs = []
    corpos = [abs(c['c'] - c['o']) for c in candles]
    media_corpo = sum(corpos) / len(corpos) if corpos else 0
    for i in range(len(candles) - 1):
        c, nxt = candles[i], candles[i + 1]
        corpo_nxt = abs(nxt['c'] - nxt['o'])
        if media_corpo == 0 or corpo_nxt < media_corpo * 1.5:
            continue
        up_c = c['c'] >= c['o']
        up_nxt = nxt['c'] >= nxt['o']
        if up_nxt and not up_c:
            obs.append({'tipo': 'OB_bullish', 'top': round(c['o'], 6), 'bottom': round(c['c'], 6), 't': c['t'], 'idx': i})
        elif not up_nxt and up_c:
            obs.append({'tipo': 'OB_bearish', 'top': round(c['c'], 6), 'bottom': round(c['o'], 6), 't': c['t'], 'idx': i})
    return obs[-10:]


def find_order_blocks_com_mitigacao(exec_candles, lookback=100):
    candles = exec_candles[-lookback:] if len(exec_candles) > lookback else exec_candles
    obs = find_order_blocks(exec_candles, lookback)
    for ob in obs:
        idx = ob.pop('idx', None)
        if idx is None:
            ob['mitigado'] = None
            continue
        candles_depois = candles[idx + 2:]
        mitigado = any(c['l'] <= ob['top'] and c['h'] >= ob['bottom'] for c in candles_depois)
        ob['mitigado'] = mitigado
    return obs


def find_equal_highs_lows(candles, length=3, atr_mult=0.1):
    atr_series = compute_atr(candles, 14)
    atr_atual = next((v for v in reversed(atr_series) if v is not None), None)
    if not atr_atual:
        return []
    swings = detect_exec_swings(candles, lookback=length)
    grupos = []
    for s in swings:
        colocado = False
        for g in grupos:
            if s['tipo'] == g['tipo'] and abs(s['valor'] - g['nivel']) < atr_mult * atr_atual:
                g['pontos'].append(s['valor'])
                g['nivel'] = sum(g['pontos']) / len(g['pontos'])
                colocado = True
                break
        if not colocado:
            grupos.append({'tipo': s['tipo'], 'nivel': s['valor'], 'pontos': [s['valor']]})
    return [
        {'tipo': 'EQH' if g['tipo'] == 'high' else 'EQL', 'nivel': round(g['nivel'], 6), 'toques': len(g['pontos'])}
        for g in grupos if len(g['pontos']) >= 2
    ]


def debug_zonas_completo(d1_candles, exec_candles):
    return {
        'zonas_sr_channel_d1': compute_d1_zones(d1_candles),
        'zona_diaria': compute_zona_diaria_movel(d1_candles),
        'premium_discount': compute_premium_discount(exec_candles),
        'fvgs_abertas': find_open_fvgs(exec_candles),
        'order_blocks_recentes': find_order_blocks(exec_candles),
        'liquidez_eqh_eql': find_equal_highs_lows(exec_candles),
    }


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
    for i in range(len(exec_candles) - 1, max(0, len(exec_candles) - 30), -1):
        c = exec_candles[i]
        if c['h'] > zona['top'] and c['c'] < zona['top']:
            return {'index': i, 'lado': 'alta', 'nivel': c['h'], 't': c['t']}
        if c['l'] < zona['bottom'] and c['c'] > zona['bottom']:
            return {'index': i, 'lado': 'baixa', 'nivel': c['l'], 't': c['t']}
    return None


def detect_choch_after_sweep(exec_candles, sweep):
    swings = detect_exec_swings(exec_candles)
    ref = None
    for s in swings:
        if s['t'] <= sweep['t']:
            continue
        if sweep['lado'] == 'baixa' and s['tipo'] == 'high':
            ref = s
            break
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


def detect_bos_continuation_after_sweep(exec_candles, sweep):
    for i, c in enumerate(exec_candles):
        if c['t'] <= sweep['t']:
            continue
        if sweep['lado'] == 'baixa' and c['c'] < sweep['nivel']:
            return {'index': i, 'direcao': 'baixa', 'nivel': sweep['nivel'], 't': c['t']}
        if sweep['lado'] == 'alta' and c['c'] > sweep['nivel']:
            return {'index': i, 'direcao': 'alta', 'nivel': sweep['nivel'], 't': c['t']}
    return None


MICRO_BOS_LOOKBACK = 3


def detect_micro_bos(exec_candles, direcao, lookback=MICRO_BOS_LOOKBACK):
    if not exec_candles or len(exec_candles) < lookback + 2:
        return {'confirmado': False, 'nivel_rompido': None}

    janela_recente = exec_candles[-lookback:]
    candles_antes = exec_candles[:-lookback]
    if not candles_antes:
        return {'confirmado': False, 'nivel_rompido': None}

    ref_lookback = min(10, len(candles_antes))
    candles_ref = candles_antes[-ref_lookback:]

    if direcao == 'alta':
        topo_local = max(c['h'] for c in candles_ref)
        rompeu = any(c['c'] > topo_local for c in janela_recente)
        return {'confirmado': rompeu, 'nivel_rompido': round(topo_local, 6) if rompeu else None}
    else:
        fundo_local = min(c['l'] for c in candles_ref)
        rompeu = any(c['c'] < fundo_local for c in janela_recente)
        return {'confirmado': rompeu, 'nivel_rompido': round(fundo_local, 6) if rompeu else None}
def find_fvg_ob_after_choch(exec_candles, choch, min_gap_pct=MIN_FVG_GAP_PCT):
    start = max(0, choch['index'] - 1)
    end = min(len(exec_candles) - 1, choch['index'] + 4)

    for i in range(start + 1, end):
        if i + 1 >= len(exec_candles):
            break
        prev, nxt = exec_candles[i - 1], exec_candles[i + 1]
        if choch['direcao'] == 'alta' and nxt['l'] > prev['h']:
            gap_pct = (nxt['l'] - prev['h']) / prev['h'] if prev['h'] else 0
            if gap_pct >= min_gap_pct:
                return {'tipo': 'FVG', 'top': nxt['l'], 'bottom': prev['h']}
        if choch['direcao'] == 'baixa' and nxt['h'] < prev['l']:
            gap_pct = (prev['l'] - nxt['h']) / prev['l'] if prev['l'] else 0
            if gap_pct >= min_gap_pct:
                return {'tipo': 'FVG', 'top': prev['l'], 'bottom': nxt['h']}

    for i in range(choch['index'], max(0, choch['index'] - 6), -1):
        c = exec_candles[i]
        up = c['c'] >= c['o']
        if choch['direcao'] == 'alta' and not up:
            return {'tipo': 'OB', 'top': c['o'], 'bottom': c['c']}
        if choch['direcao'] == 'baixa' and up:
            return {'tipo': 'OB', 'top': c['c'], 'bottom': c['o']}
    return None


def find_ifvg_after_choch(exec_candles, choch):
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


def melhor_preco_na_zona(entry_zone, direcao, preco_atual_fallback=None):
    if not entry_zone or entry_zone.get('top') is None or entry_zone.get('bottom') is None:
        return preco_atual_fallback
    return entry_zone['bottom'] if direcao == 'alta' else entry_zone['top']


def candle_e_decisivo(candle, min_body_ratio=MIN_CANDLE_BODY_RATIO):
    range_total = candle['h'] - candle['l']
    if range_total <= 0:
        return True
    corpo = abs(candle['c'] - candle['o'])
    return (corpo / range_total) >= min_body_ratio


def aplicar_buffer_stop(nivel, direcao, buffer_pct=STOP_BUFFER_PCT):
    if direcao == 'alta':
        return nivel * (1 - buffer_pct)
    return nivel * (1 + buffer_pct)


ATR_BUFFER_MULT = 0.25


def aplicar_buffer_stop_atr(nivel, direcao, exec_candles, atr_mult=ATR_BUFFER_MULT, fallback_pct=STOP_BUFFER_PCT):
    try:
        atr_series = compute_atr(exec_candles, 14)
        atr_atual = next((v for v in reversed(atr_series) if v is not None), None)
    except Exception:
        atr_atual = None

    if atr_atual is None or atr_atual <= 0:
        return aplicar_buffer_stop(nivel, direcao, fallback_pct)

    folga = atr_atual * atr_mult
    if direcao == 'alta':
        return nivel - folga
    return nivel + folga


def _load_saved_state(db_file, pair, table='scalp_zone_state'):
    try:
        with sqlite3.connect(db_file) as conn:
            cursor = conn.cursor()
            cursor.execute(f'''
                SELECT zona_top, zona_bottom, sweep_ts, sweep_nivel, sweep_lado, updated_at
                FROM {table} WHERE pair=?
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
        print(f"[scalp_engine] erro ao carregar estado salvo de {pair} ({table}): {e}")
        return None


def _sweep_ainda_valido(saved, zona, now):
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


def compute_ema(values, period):
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


def compute_adx_com_direcao(candles, period=14):
    n = len(candles)
    if n < period * 2 + 2:
        return None, None

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
        return None, None
    idx_primeiro_adx = period * 2 - 1
    adx[idx_primeiro_adx] = sum(janela_inicial) / period
    for i in range(idx_primeiro_adx + 1, n):
        if dx[i] is None:
            continue
        adx[i] = (adx[i - 1] * (period - 1) + dx[i]) / period

    adx_atual = next((v for v in reversed(adx) if v is not None), None)
    if adx_atual is None:
        return None, None

    idx_atual = len(adx) - 1
    while idx_atual >= 0 and adx[idx_atual] is None:
        idx_atual -= 1
    if idx_atual < 0 or atr_s[idx_atual] is None or not atr_s[idx_atual]:
        return round(adx_atual, 2), None

    pdi_final = 100 * plus_di_s[idx_atual] / atr_s[idx_atual]
    mdi_final = 100 * minus_di_s[idx_atual] / atr_s[idx_atual]
    direcao = 'alta' if pdi_final > mdi_final else 'baixa'

    return round(adx_atual, 2), direcao


def compute_bollinger(closes, period=20, std_mult=2):
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
    if not exec_candles:
        return None
    dia_atual = datetime.fromtimestamp(exec_candles[-1]['t'] / 1000, tz=timezone.utc).date()
    cum_pv, cum_vol = 0.0, 0.0
    for c in exec_candles:
        if datetime.fromtimestamp(c['t'] / 1000, tz=timezone.utc).date() != dia_atual:
            continue
        typical = (c['h'] + c['l'] + c['c']) / 3
        vol = c.get('v', 0)
        cum_pv += typical * vol
        cum_vol += vol
    if cum_vol == 0:
        return None
    return round(cum_pv / cum_vol, 6)


def compute_volume_profile_poc(exec_candles, lookback=100, bins=24):
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


def _ema_slope(series, back=5):
    if len(series) <= back:
        return None
    atual, antigo = series[-1], series[-1 - back]
    if atual is None or antigo is None:
        return None
    return atual - antigo


def compute_technical_indicators(exec_candles):
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
        'ema9_slope': _ema_slope(ema9),
        'macd_line': last(macd_line), 'macd_signal': last(macd_signal), 'macd_hist': last(macd_hist),
        'atr14': last(atr_series),
        'adx14': last(adx_series),
        'bollinger_upper': last(bb_upper), 'bollinger_mid': last(bb_mid), 'bollinger_lower': last(bb_lower),
        'stoch_k': last(stoch_k), 'stoch_d': last(stoch_d),
    }

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
    detalhes = []
    score = 0

    pts_zona = 20 if zona['toques'] >= 3 else 15
    score += pts_zona
    detalhes.append(('banda_d1', pts_zona))

    if na_killzone:
        score += 10
        detalhes.append(('dentro_killzone', 10))

    score += 25
    detalhes.append(('sweep_choch', 25))

    if entry_zone['tipo'] in ('FVG', 'iFVG'):
        pts_fvg_ob = 20
    elif entry_zone['tipo'] == 'Breaker':
        pts_fvg_ob = 18
    else:
        pts_fvg_ob = 15
    score += pts_fvg_ob
    detalhes.append(('fvg_ob_retorno', pts_fvg_ob))

    vols = [c.get('v', 0) for c in exec_candles[-20:]]
    if vols:
        media_vol = sum(vols) / len(vols)
        choch_candle = exec_candles[choch['index']] if choch['index'] < len(exec_candles) else None
        if choch_candle and choch_candle.get('v', 0) > media_vol * 1.3:
            score += 8
            detalhes.append(('volume_choch_forte', 8))

    return min(score, 100), detalhes


def find_liquidez_antiga(exec_candles, ate_index, tipo, lookback=None):
    lookback = lookback or LIQUIDEZ_LOOKBACK
    inicio = max(0, ate_index - lookback)
    janela = exec_candles[inicio:ate_index - 2]
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


def detect_sweep_liquidez_antiga(exec_candles, zona, lookback=None):
    lookback = lookback or ANTECIPADO_SWEEP_LOOKBACK
    recentes_idx = range(max(0, len(exec_candles) - lookback), len(exec_candles))

    for i in recentes_idx:
        c = exec_candles[i]

        if c['h'] >= zona['top']:
            liq_antiga, liq_index = find_liquidez_antiga(exec_candles, i, 'high')
            if liq_antiga and c['h'] > liq_antiga and c['c'] < liq_antiga:
                return {
                    'index': i, 'lado': 'baixa',
                    'nivel_pavio': c['h'],
                    'liquidez_varrida': liq_antiga,
                    'liquidez_index': liq_index,
                    't': c['t'],
                }

        if c['l'] <= zona['bottom']:
            liq_antiga, liq_index = find_liquidez_antiga(exec_candles, i, 'low')
            if liq_antiga and c['l'] < liq_antiga and c['c'] > liq_antiga:
                return {
                    'index': i, 'lado': 'alta',
                    'nivel_pavio': c['l'],
                    'liquidez_varrida': liq_antiga,
                    'liquidez_index': liq_index,
                    't': c['t'],
                }

    return None


def compute_bias_from_swings(candles, lookback=SWING_LOOKBACK):
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
    closes = [c['c'] for c in exec_candles[:idx + 1]]
    if len(closes) < 15:
        return None
    rsi_series = compute_rsi(closes)
    return rsi_series[-1]


def rsi_no_candle(exec_candles, idx):
    if idx is None or idx < 0:
        return None
    closes = [c['c'] for c in exec_candles[:idx + 1]]
    if len(closes) < 15:
        return None
    rsi_series = compute_rsi(closes)
    return rsi_series[-1]


def check_rsi_divergence(exec_candles, sweep):
    liq_index = sweep.get('liquidez_index')
    if liq_index is None:
        return False

    rsi_liquidez_antiga = rsi_no_candle(exec_candles, liq_index)
    rsi_sweep_atual = rsi_no_candle(exec_candles, sweep['index'])
    if rsi_liquidez_antiga is None or rsi_sweep_atual is None:
        return False

    if sweep['lado'] == 'alta':
        preco_igual_ou_mais_baixo = sweep['nivel_pavio'] <= sweep['liquidez_varrida']
        rsi_mais_alto = rsi_sweep_atual > rsi_liquidez_antiga
        return preco_igual_ou_mais_baixo and rsi_mais_alto

    if sweep['lado'] == 'baixa':
        preco_igual_ou_mais_alto = sweep['nivel_pavio'] >= sweep['liquidez_varrida']
        rsi_mais_baixo = rsi_sweep_atual < rsi_liquidez_antiga
        return preco_igual_ou_mais_alto and rsi_mais_baixo

    return False


def _save_zone_state(db_file, pair, zona, fase, now, sweep=None, choch=None, table='scalp_zone_state'):
    try:
        with sqlite3.connect(db_file) as conn:
            cursor = conn.cursor()
            cursor.execute(f'''
                INSERT INTO {table} (pair, zona_top, zona_bottom, fase, sweep_ts, sweep_nivel, sweep_lado, choch_ts, choch_nivel, updated_at)
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
        print(f"[scalp_engine] erro ao salvar zone_state de {pair} ({table}): {e}")


def _segundos_desde_ultimo_alerta(db_file, table, pair):
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


def _save_signal(db_file, pair, exec_tf_label, resultado, alerted, table='scalp_signal_state'):
    try:
        prefixo = 'cont' if table == 'scalp_signal_state_continuacao' else 'scalp'
        signal_id = f"{prefixo}_{pair}_{int(time.time()*1000)}"

        detalhes = resultado.get('detalhes') or []
        motivo_texto = ','.join(f"{nome}:{pts}" for nome, pts in detalhes)

        with sqlite3.connect(db_file) as conn:
            cursor = conn.cursor()
            cursor.execute(f'''
                INSERT INTO {table}
                    (id, pair, created_at, exec_tf, direcao, score, entry, sl, tp, na_killzone, alerted, motivo_score)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ''', (
                signal_id, pair, int(time.time()), exec_tf_label,
                resultado['direcao'], resultado['score'], resultado['entry'], resultado['sl'], resultado['tp'],
                1 if resultado['na_killzone'] else 0, 1 if alerted else 0, motivo_texto,
            ))
            conn.commit()
    except Exception as e:
        print(f"[scalp_engine] erro ao salvar signal de {pair} ({table}): {e}")


def scalp_signal_history(db_file, pair=None, limit=30, table='scalp_signal_state'):
    try:
        with sqlite3.connect(db_file) as conn:
            cursor = conn.cursor()
            if pair:
                cursor.execute(f'''
                    SELECT id, pair, created_at, exec_tf, direcao, score, entry, sl, tp, na_killzone, alerted
                    FROM {table} WHERE pair=? ORDER BY created_at DESC LIMIT ?
                ''', (pair, limit))
            else:
                cursor.execute(f'''
                    SELECT id, pair, created_at, exec_tf, direcao, score, entry, sl, tp, na_killzone, alerted
                    FROM {table} ORDER BY created_at DESC LIMIT ?
                ''', (limit,))
            rows = cursor.fetchall()

        signals = []
        for r in rows:
            signals.append({
                'id': r[0], 'pair': r[1], 'created_at': r[2], 'exec_tf': r[3],
                'direcao': r[4], 'score': r[5], 'entry': r[6], 'sl': r[7], 'tp': r[8],
                'na_killzone': bool(r[9]), 'alerted': bool(r[10]),
            })
        return {'signals': signals}
    except Exception as e:
        print(f"[scalp_engine] erro ao gerar histórico de {pair} ({table}): {e}")
        return {'signals': [], 'error': str(e)}
# ═══════════════════════════════════════════════════════════════════════
# TP DINÂMICO — decisão explícita do usuário (10/08): nada de RR fixo
# tipo "sempre 2.5x". O alvo passa a vir da análise real do gráfico —
# a próxima liquidez de verdade (zona D1 oposta, Order Block, Equal
# Highs/Lows) que o preço tem motivo real pra buscar. Só cai pro
# Monte Carlo real (nunca decorativo) ou pro RR mínimo se não houver
# nenhuma liquidez real mapeada na direção do trade.
# ═══════════════════════════════════════════════════════════════════════

TP_DINAMICO_MAX_RR = 6.0  # teto de sanidade — nunca mira mais que isso


def _find_liquidez_alvo(direcao, entry, exec_candles, d1_candles):
    """Levanta candidatos reais de alvo (liquidez) na direção do trade:
    Equal Highs/Lows, zona D1 oposta, Order Blocks."""
    candidatos = []

    try:
        for eq in find_equal_highs_lows(exec_candles):
            nivel = eq['nivel']
            if direcao == 'alta' and nivel > entry:
                candidatos.append((eq['tipo'], nivel))
            elif direcao == 'baixa' and nivel < entry:
                candidatos.append((eq['tipo'], nivel))
    except Exception:
        pass

    try:
        for banda in compute_d1_zones(d1_candles):
            if direcao == 'alta' and banda['top'] > entry:
                candidatos.append(('zona_d1', banda['top']))
            elif direcao == 'baixa' and banda['bottom'] < entry:
                candidatos.append(('zona_d1', banda['bottom']))
    except Exception:
        pass

    try:
        for ob in find_order_blocks(exec_candles):
            if direcao == 'alta' and ob['bottom'] > entry:
                candidatos.append(('OB', ob['bottom']))
            elif direcao == 'baixa' and ob['top'] < entry:
                candidatos.append(('OB', ob['top']))
    except Exception:
        pass

    return candidatos


def calcular_tp_dinamico(direcao, entry, sl, exec_candles, d1_candles, min_rr=None):
    """
    Devolve (tp, origem_texto). Prioridade:
    1. Liquidez real mais próxima (EQH/EQL, zona D1, OB) que já renda
       pelo menos min_rr — pega a MAIS PRÓXIMA, não a mais distante,
       porque é a que o preço tem mais chance de alcançar primeiro.
    2. Monte Carlo real (cenário otimista/pessimista da simulação),
       se render pelo menos min_rr.
    3. Só em último caso, RR mínimo puro sobre o risco real do setup.
    """
    min_rr = min_rr if min_rr is not None else MIN_RR_GATE
    risco = abs(entry - sl)
    if risco <= 0:
        return None, 'sem_risco_valido'

    validos = []
    for origem, nivel in _find_liquidez_alvo(direcao, entry, exec_candles, d1_candles):
        distancia = abs(nivel - entry)
        rr = distancia / risco
        if min_rr <= rr <= TP_DINAMICO_MAX_RR:
            validos.append((origem, nivel, rr))

    if validos:
        validos.sort(key=lambda x: x[2])  # mais próximo primeiro
        origem, nivel, rr = validos[0]
        return nivel, f'liquidez_real:{origem} (RR {rr:.2f})'

    try:
        mc = compute_monte_carlo(exec_candles)
    except Exception:
        mc = None
    if mc:
        alvo_mc = mc['cenario_otimista'] if direcao == 'alta' else mc['cenario_pessimista']
        distancia = abs(alvo_mc - entry)
        rr = distancia / risco
        if rr >= min_rr:
            return alvo_mc, f'monte_carlo (RR {rr:.2f})'

    tp_fallback = entry + risco * min_rr if direcao == 'alta' else entry - risco * min_rr
    return tp_fallback, f'fallback_rr_minimo ({min_rr})'


# ═══════════════════════════════════════════════════════════════════════
# MULTI-ATIVO — Bias (NY Midnight Open) + SFP (Swing Failure Pattern
# contra sessão anterior) + MSS + FVG, com validação estrita em
# sequência (cada passo tem que confirmar o anterior, sem gambiarra de
# soma de pontos). Funciona pra qualquer ativo que alimente candles —
# XAU/USD ainda não tem fonte de dado real conectada no backend, mas a
# lógica em si já roda igual pra XAU e pra Cripto assim que a fonte
# existir.
# ═══════════════════════════════════════════════════════════════════════

PARES_METAL = {'XAUUSD', 'XAUUSDT', 'GOLDUSD', 'GOLDUSDT', 'PAXGUSDT'}

ASSET_PROFILES = {
    'metal': {'wick_buffer_mult': 1.0},
    'crypto': {'wick_buffer_mult': 1.8},  # cripto pavia bem mais forte que XAU/forex
}

NY_TZ = ZoneInfo('America/New_York')

SESSOES_UTC = {
    'asia': (0, 8),      # 00:00–08:00 UTC
    'london': (7, 16),   # 07:00–16:00 UTC (cobre o killzone de Londres)
}


def get_asset_class(pair):
    return 'metal' if pair.upper() in PARES_METAL else 'crypto'


def get_asset_profile(pair):
    return ASSET_PROFILES[get_asset_class(pair)]


def _find_open_at_hour(candles, hora_alvo, tz):
    """Acha o open do candle mais recente que abriu na hora alvo, no
    fuso horário indicado — serve de 'linha de água' de referência
    (Midnight Open)."""
    melhor = None
    for c in candles:
        dt = datetime.fromtimestamp(c['t'] / 1000, tz=timezone.utc).astimezone(tz)
        if dt.hour == hora_alvo and dt.minute < 5:
            melhor = c
    return melhor['o'] if melhor else None


def compute_midnight_open_utc(candles):
    """UTC Midnight Open — abertura do candle das 00:00 UTC."""
    return _find_open_at_hour(candles, 0, timezone.utc)


def compute_midnight_open_ny(candles):
    """NY Midnight Open — abertura às 00:00 America/New_York (DST correto
    via zoneinfo, sem offset fixo que quebra no horário de verão)."""
    return _find_open_at_hour(candles, 0, NY_TZ)


def compute_bias_midnight_open(candles, midnight_open_fn=compute_midnight_open_ny):
    """Bias de Compra/Venda: preço atual acima ou abaixo do Midnight
    Open. Retorna ('alta'|'baixa'|None, midnight_open)."""
    if not candles:
        return None, None
    midnight_open = midnight_open_fn(candles)
    if midnight_open is None:
        return None, None
    preco_atual = candles[-1]['c']
    bias = 'alta' if preco_atual > midnight_open else 'baixa'
    return bias, midnight_open


def compute_session_high_low(candles, sessao, dias_atras=1):
    """High/Low de uma sessão (Ásia ou Londres) de N dias atrás — nível
    de liquidez real que o SFP vai testar."""
    if sessao not in SESSOES_UTC or not candles:
        return None
    inicio_h, fim_h = SESSOES_UTC[sessao]
    hoje_utc = datetime.fromtimestamp(candles[-1]['t'] / 1000, tz=timezone.utc).date()
    dia_alvo = hoje_utc - timedelta(days=dias_atras)

    highs, lows = [], []
    for c in candles:
        dt = datetime.fromtimestamp(c['t'] / 1000, tz=timezone.utc)
        if dt.date() == dia_alvo and inicio_h <= dt.hour < fim_h:
            highs.append(c['h'])
            lows.append(c['l'])
    if not highs:
        return None
    return {'high': max(highs), 'low': min(lows), 'sessao': sessao, 'dia': str(dia_alvo)}


def detect_sfp_sessao(exec_candles, session_range):
    """
    Swing Failure Pattern contra o High/Low de uma sessão específica:
    candle que ultrapassa o nível com o pavio mas FECHA de volta dentro
    — a assinatura clássica de caça de liquidez SMC. Procura nos
    últimos candles (mais recente primeiro).
    """
    if not session_range:
        return None
    high, low = session_range['high'], session_range['low']
    for i in range(len(exec_candles) - 1, max(0, len(exec_candles) - 40), -1):
        c = exec_candles[i]
        if c['h'] > high and c['c'] < high:
            return {
                'index': i, 'tipo': 'SFP_bearish', 'nivel': high,
                'sl_nivel': c['h'], 't': c['t'], 'sessao': session_range['sessao'],
            }
        if c['l'] < low and c['c'] > low:
            return {
                'index': i, 'tipo': 'SFP_bullish', 'nivel': low,
                'sl_nivel': c['l'], 't': c['t'], 'sessao': session_range['sessao'],
            }
    return None


def compute_wick_atr(candles, period=14):
    """ATR calculado só em cima do tamanho dos pavios (rejeição), não do
    range total do candle. Cripto costuma ter pavios muito mais longos
    que XAU/forex — um ATR de corpo comum fica curto demais pro stop."""
    n = len(candles)
    if n < period + 1:
        return None
    pavios = []
    for c in candles[-period:]:
        corpo_top = max(c['o'], c['c'])
        corpo_bottom = min(c['o'], c['c'])
        pavio_sup = c['h'] - corpo_top
        pavio_inf = corpo_bottom - c['l']
        pavios.append(max(pavio_sup, pavio_inf))
    return sum(pavios) / len(pavios) if pavios else None


def aplicar_buffer_stop_multiativo(nivel, direcao, exec_candles, pair, fallback_pct=STOP_BUFFER_PCT):
    """Buffer de stop ajustado pela CLASSE do ativo. Cripto usa o pavio
    médio (wick ATR) multiplicado pelo perfil do ativo; XAU/metal usa o
    ATR normal, que já é adequado pro comportamento mais 'liso' dele."""
    classe = get_asset_class(pair)
    perfil = get_asset_profile(pair)

    if classe == 'crypto':
        wick_atr = compute_wick_atr(exec_candles, 14)
        if wick_atr and wick_atr > 0:
            folga = wick_atr * perfil['wick_buffer_mult']
            return nivel - folga if direcao == 'alta' else nivel + folga
        return aplicar_buffer_stop_atr(nivel, direcao, exec_candles, fallback_pct=fallback_pct)

    return aplicar_buffer_stop_atr(nivel, direcao, exec_candles, fallback_pct=fallback_pct)


# ═══════════════════════════════════════════════════════════════════════
# PIPELINE SMC/ICT ESTRITO — Bias (Midnight Open) → SFP → MSS (M1) → FVG.
# Só retorna sinal quando 100% das condições passarem em sequência; se
# qualquer passo falhar ou for invalidado (breakout real), devolve
# status NEUTRAL (equivalente a NULL) — nunca força um sinal.
#
# Funciona igual pra XAU/USD e Cripto (BTC/ETH/SOL/...) — só troca a
# fonte do Midnight Open (NY pro ouro, UTC pra cripto) e a janela de
# liquidez (sessão anterior pro ouro, últimos 3-7 dias pra cripto).
# ═══════════════════════════════════════════════════════════════════════

SFP_LIQUIDEZ_COOLDOWN_SECONDS = 45 * 60
XAU_PIP_SIZE = 0.01  # ajusta aqui se a tua corretora usar outra convenção de pip pro XAU
XAU_SL_BUFFER_PIPS = 2
CRYPTO_SL_BUFFER_PCT = 0.002  # 0.2%
MSS_CORPO_MIN_PCT = 0.5  # corpo/range mínimo pra considerar "momentum forte"
TP1_MIN_RR = 2.0
TP2_MIN_RR = 3.0


def init_sfp_liquidez_db(db_file):
    with sqlite3.connect(db_file) as conn:
        conn.execute('''
            CREATE TABLE IF NOT EXISTS scalp_sfp_liquidez_signal_state (
                id TEXT PRIMARY KEY,
                pair TEXT,
                created_at INTEGER,
                exec_tf TEXT,
                direcao TEXT,
                entry REAL,
                sl REAL,
                tp1 REAL,
                tp2 REAL,
                bias_context TEXT,
                resultado_final TEXT DEFAULT 'pendente',
                alerted INTEGER DEFAULT 0
            )
        ''')
        conn.commit()


def _save_sfp_liquidez_signal(db_file, pair, exec_tf_label, resultado, alerted):
    try:
        signal_id = f"sfp_{pair}_{int(time.time()*1000)}"
        with sqlite3.connect(db_file) as conn:
            conn.execute('''
                INSERT INTO scalp_sfp_liquidez_signal_state
                    (id, pair, created_at, exec_tf, direcao, entry, sl, tp1, tp2, bias_context, alerted)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ''', (
                signal_id, pair, int(time.time()), exec_tf_label,
                resultado['direcao'], resultado['entry'], resultado['sl'],
                resultado['tp1'], resultado['tp2'], resultado.get('bias_context'),
                1 if alerted else 0,
            ))
            conn.commit()
    except Exception as e:
        print(f"[scalp_engine sfp_liquidez] erro ao salvar sinal de {pair}: {e}")


# ── PASSO 1: Bias via Midnight Open ─────────────────────────────────────

def compute_bias_midnight_open_estrito(pair, candles_por_tf):
    """
    Bias ESTRITO de reversão (consequent encroachment ICT):
    - Preço ACIMA do Midnight Open -> só procura SHORT.
    - Preço ABAIXO do Midnight Open -> só procura LONG.
    XAU usa NY Midnight Open (~05:00 WET, calculado via zoneinfo com DST
    correto — equivale ao "05:00 UTC+1" pedido, mas sem quebrar no
    horário de verão). Cripto usa UTC Midnight Open (00:00 UTC).

    Retorna (direcao_permitida, midnight_open, bias_context) — qualquer
    um None se não der pra calcular.
    """
    classe = get_asset_class(pair)
    candles_ref = candles_por_tf.get('M15') or candles_por_tf.get('M5') or candles_por_tf.get('H1')
    if not candles_ref:
        return None, None, None

    midnight_open = compute_midnight_open_ny(candles_ref) if classe == 'metal' else compute_midnight_open_utc(candles_ref)
    if midnight_open is None:
        return None, None, None

    preco_atual = candles_ref[-1]['c']
    if preco_atual > midnight_open:
        return 'baixa', midnight_open, 'ACIMA_MIDNIGHT_OPEN'
    return 'alta', midnight_open, 'ABAIXO_MIDNIGHT_OPEN'


# ── PASSO 2: Liquidez de referência + SFP com cancelamento por breakout ──

def compute_liquidez_referencia(pair, candles_por_tf):
    """
    XAU: High/Low da sessão anterior (Londres, fallback Ásia).
    Cripto: High/Low dos últimos 3-7 dias (D1), excluindo o dia em curso.
    Devolve também 'cutoff_ts' — só candles depois desse timestamp
    contam pra validação do SFP (não pode usar candle que ainda fez
    parte da própria formação da liquidez).
    """
    classe = get_asset_class(pair)

    if classe == 'metal':
        candles_sessao = candles_por_tf.get('M15') or candles_por_tf.get('H1')
        if not candles_sessao:
            return None
        for sessao in ('london', 'asia'):
            liquidez = compute_session_high_low(candles_sessao, sessao, dias_atras=1)
            if liquidez:
                candles_da_sessao = [
                    c for c in candles_sessao
                    if datetime.fromtimestamp(c['t'] / 1000, tz=timezone.utc).date().isoformat() == liquidez['dia']
                    and SESSOES_UTC[sessao][0] <= datetime.fromtimestamp(c['t'] / 1000, tz=timezone.utc).hour < SESSOES_UTC[sessao][1]
                ]
                liquidez['cutoff_ts'] = max(c['t'] for c in candles_da_sessao) if candles_da_sessao else None
                liquidez['candles_liquidez'] = candles_da_sessao
                return liquidez
        return None

    # cripto: últimos 3-7 dias em D1
    d1 = candles_por_tf.get('D1')
    if not d1 or len(d1) < 4:
        return None
    janela = d1[-8:-1] if len(d1) >= 8 else d1[:-1]
    if len(janela) < 3:
        return None
    return {
        'high': max(c['h'] for c in janela),
        'low': min(c['l'] for c in janela),
        'sessao': f'ultimos_{len(janela)}_dias', 'dia': None,
        'cutoff_ts': janela[-1]['t'],
        'candles_liquidez': janela,
    }


def validar_sfp_estrito(candles_sfp, liquidez, direcao_permitida):
    """
    Varre os candles CRONOLOGICAMENTE a partir do fim da janela de
    liquidez. O primeiro evento que acontecer decide tudo:
    - Fechamento de CORPO além do nível -> Breakout real, CANCELA a
      análise (retorna None, motivo 'breakout_cancela_analise').
    - Pavio ultrapassa mas fecha de volta dentro -> SFP confirmado.
    Só olha o lado relevante pra direção permitida pelo Bias (Passo 1).
    """
    if not liquidez:
        return None, 'sem_liquidez_mapeada'

    cutoff_ts = liquidez.get('cutoff_ts')
    candles_pos = [c for c in candles_sfp if cutoff_ts is None or c['t'] > cutoff_ts]
    high_liq, low_liq = liquidez['high'], liquidez['low']

    for c in candles_pos:
        if direcao_permitida == 'baixa':  # bias pede SHORT -> só liquidez de topo interessa
            if c['c'] > high_liq:
                return None, 'breakout_cancela_analise'
            if c['h'] > high_liq and c['c'] < high_liq:
                return {'tipo': 'SFP_venda', 'nivel': high_liq, 'sl_pavio': c['h'], 't': c['t']}, 'sfp_confirmado'
        elif direcao_permitida == 'alta':  # bias pede LONG -> só liquidez de fundo interessa
            if c['c'] < low_liq:
                return None, 'breakout_cancela_analise'
            if c['l'] < low_liq and c['c'] > low_liq:
                return {'tipo': 'SFP_compra', 'nivel': low_liq, 'sl_pavio': c['l'], 't': c['t']}, 'sfp_confirmado'

    return None, 'sem_sfp_ainda'


def validar_sfp_cascata_tf(candles_por_tf, liquidez, direcao_permitida):
    """
    Tenta achar o SFP em cascata: M15 primeiro (sweep mais confiável,
    menos ruído), se não achar cai pro M5, se não achar cai pro M1 —
    maximiza oportunidade sem abrir mão de tentar o TF mais limpo
    primeiro. Se qualquer TF confirmar breakout real, cancela na hora
    (não faz sentido procurar SFP num nível que já foi rompido de vez).
    Retorna (sfp, motivo, timeframe_usado).
    """
    ordem_tfs = ['M15', 'M5', 'M1']
    ultimo_motivo = 'sem_candles_sfp'

    for tf_label in ordem_tfs:
        candles_tf = candles_por_tf.get(tf_label)
        if not candles_tf:
            continue

        sfp, motivo = validar_sfp_estrito(candles_tf, liquidez, direcao_permitida)

        if sfp:
            return sfp, motivo, tf_label

        if motivo == 'breakout_cancela_analise':
            return None, motivo, tf_label

        ultimo_motivo = motivo

    return None, ultimo_motivo, None


# ── PASSO 3: MSS confirmado no M1, com corpo forte ──────────────────────

def validar_mss_m1(candles_m1, sfp, direcao_permitida, corpo_min_pct=MSS_CORPO_MIN_PCT):
    """
    Exige fechamento de corpo (não só pavio) quebrando o último
    fundo/topo relevante em M1, na direção do SFP, com momentum real
    (corpo/range >= corpo_min_pct — filtra rompimentos fracos/indecisos).
    """
    if not candles_m1:
        return None

    candles_pos_sfp = [c for c in candles_m1 if c['t'] > sfp['t']]
    if len(candles_pos_sfp) < 5:
        return None

    swings_m1 = detect_exec_swings(candles_pos_sfp, lookback=3)
    tipo_ref = 'low' if direcao_permitida == 'baixa' else 'high'
    referencias = [s for s in swings_m1 if s['tipo'] == tipo_ref]
    if not referencias:
        return None
    ref = referencias[0]

    for i, c in enumerate(candles_pos_sfp):
        if c['t'] <= ref['t']:
            continue
        range_total = c['h'] - c['l']
        corpo = abs(c['c'] - c['o'])
        corpo_forte = range_total > 0 and (corpo / range_total) >= corpo_min_pct

        if direcao_permitida == 'baixa' and c['c'] < ref['valor'] and corpo_forte:
            return {'index': i, 'direcao': 'baixa', 'nivel': ref['valor'], 't': c['t'], 'candles_ref': candles_pos_sfp}
        if direcao_permitida == 'alta' and c['c'] > ref['valor'] and corpo_forte:
            return {'index': i, 'direcao': 'alta', 'nivel': ref['valor'], 't': c['t'], 'candles_ref': candles_pos_sfp}

    return None


# ── PASSO 4: POI (FVG), entrada 50%, SL/TP por classe de ativo ──────────

def calcular_sl_estrito(pair, sfp):
    """SL por classe de ativo, exatamente como especificado:
    XAU = pavio do SFP + buffer de pips. Cripto = pavio do SFP * 0.2%."""
    classe = get_asset_class(pair)
    venda = sfp['tipo'] == 'SFP_venda'

    if classe == 'metal':
        buffer = XAU_SL_BUFFER_PIPS * XAU_PIP_SIZE
        return sfp['sl_pavio'] + buffer if venda else sfp['sl_pavio'] - buffer

    fator = (1 + CRYPTO_SL_BUFFER_PCT) if venda else (1 - CRYPTO_SL_BUFFER_PCT)
    return sfp['sl_pavio'] * fator


def _buscar_alvo_eqh_eql(direcao, entry, risco, candles_liquidez, min_rr):
    """TP2 — Equal Highs/Lows da própria janela de liquidez (sessão
    anterior no XAU, últimos dias na cripto), do lado oposto ao SFP."""
    if not candles_liquidez or risco <= 0:
        return None, None
    try:
        eqs = find_equal_highs_lows(candles_liquidez)
    except Exception:
        return None, None

    candidatos = []
    for eq in eqs:
        nivel = eq['nivel']
        if direcao == 'alta' and nivel > entry:
            candidatos.append(nivel)
        elif direcao == 'baixa' and nivel < entry:
            candidatos.append(nivel)

    validos = [(n, abs(n - entry) / risco) for n in candidatos if abs(n - entry) / risco >= min_rr]
    if not validos:
        return None, None
    validos.sort(key=lambda x: x[1])
    nivel, rr = validos[0]
    return nivel, rr




def formatar_saida_kairos_json(resultado):
    """Formato de saída EXATO pedido pra alimentar a interface do Kairos."""
    if resultado.get('status') != 'SIGNAL_DISPARADO':
        return {'status': 'NEUTRAL', 'ativo': resultado.get('pair'), 'motivo': resultado.get('motivo')}

    return {
        'status': 'SIGNAL_DISPARADO',
        'ativo': resultado.get('pair'),
        'direcao': 'LONG' if resultado.get('direcao') == 'alta' else 'SHORT',
        'bias_context': resultado.get('bias_context'),
        'setup': 'SFP_SWEEP + MSS + FVG_RETRACE',
        'execucao': {
            'preco_entrada': resultado.get('entry'),
            'stop_loss': resultado.get('sl'),
            'take_profit_1': resultado.get('tp1'),
            'take_profit_2': resultado.get('tp2'),
            'risco_recompensa': resultado.get('risco_recompensa'),
        },
        'fvg_zone': resultado.get('fvg_zone'),
    }


# ═══════════════════════════════════════════════════════════════════════
# PIPELINE DE GATES (A-F) — "modelo avançado da Vortex", com uma diferença
# de propósito: o Gate C (Monte Carlo) usa a simulação REAL que já existe
# no engine (compute_monte_carlo), não um número decorativo. Se a Vortex
# mostra 91% fixo, aqui o número é o que a simulação realmente calcular
# — mesmo que isso signifique o gate falhar com mais frequência.
#
# Reaproveita quase tudo que já existe: indicadores técnicos, camadas de
# score do modo '4camadas', Bias/SFP/MSS/FVG do modo 'sfp_liquidez',
# Monte Carlo, Ichimoku, TP dinâmico, SMC quality, buffer multi-ativo.
# Só duas peças novas: Supertrend e detector de Wyckoff Spring/UTAD.
# ═══════════════════════════════════════════════════════════════════════

def compute_supertrend(candles, period=10, multiplier=3.0):
    """
    Supertrend(10, 3.0) — implementação pura Python, sem pandas-ta/ta-lib
    (o backend já reimplementa todos os indicadores assim, de propósito,
    pra não depender de libs pesadas/compiladas no deploy do Railway).
    Retorna (linha, direcao) onde direcao[i] é 'alta' ou 'baixa'.
    """
    n = len(candles)
    if n < period + 1:
        return [None] * n, [None] * n

    atr_series = compute_atr(candles, period)
    hl2 = [(c['h'] + c['l']) / 2 for c in candles]

    banda_superior = [None] * n
    banda_inferior = [None] * n
    supertrend = [None] * n
    direcao = [None] * n

    for i in range(n):
        if atr_series[i] is None:
            continue
        banda_superior[i] = hl2[i] + multiplier * atr_series[i]
        banda_inferior[i] = hl2[i] - multiplier * atr_series[i]

    primeiro_valido = next((i for i in range(n) if atr_series[i] is not None), None)
    if primeiro_valido is None:
        return supertrend, direcao

    direcao[primeiro_valido] = 'alta'
    supertrend[primeiro_valido] = banda_inferior[primeiro_valido]

    for i in range(primeiro_valido + 1, n):
        if atr_series[i] is None:
            continue

        if banda_superior[i] is not None and banda_superior[i - 1] is not None:
            if candles[i - 1]['c'] > banda_superior[i - 1]:
                banda_superior[i] = min(banda_superior[i], banda_superior[i - 1])
        if banda_inferior[i] is not None and banda_inferior[i - 1] is not None:
            if candles[i - 1]['c'] < banda_inferior[i - 1]:
                banda_inferior[i] = max(banda_inferior[i], banda_inferior[i - 1])

        dir_anterior = direcao[i - 1] or 'alta'
        if dir_anterior == 'alta' and candles[i]['c'] < banda_inferior[i]:
            direcao[i] = 'baixa'
        elif dir_anterior == 'baixa' and candles[i]['c'] > banda_superior[i]:
            direcao[i] = 'alta'
        else:
            direcao[i] = dir_anterior

        supertrend[i] = banda_inferior[i] if direcao[i] == 'alta' else banda_superior[i]

    return supertrend, direcao


def detect_wyckoff_spring_utad(candles, lookback=30, tolerancia_pct=0.002):
    """
    Detector simplificado de Spring (manipulação em fundo, dentro de uma
    faixa de acumulação) e UTAD — Upthrust After Distribution (manipulação
    em topo, dentro de uma faixa de distribuição).

    Heurística: pega o range dos últimos `lookback` candles ANTES do
    candle mais recente; se o candle mais recente varre o fundo/topo
    desse range com o pavio mas fecha de volta dentro dele — e o range
    anterior tinha comportamento lateral (largura pequena relativa ao
    preço) — classifica como Spring/UTAD.
    """
    n = len(candles)
    if n < lookback + 2:
        return None

    janela = candles[-(lookback + 1):-1]
    atual = candles[-1]

    topo_range = max(c['h'] for c in janela)
    fundo_range = min(c['l'] for c in janela)
    largura_pct = (topo_range - fundo_range) / fundo_range if fundo_range else 1

    lateral = largura_pct <= 0.05  # faixa de acumulação/distribuição razoavelmente apertada

    if atual['l'] < fundo_range * (1 - tolerancia_pct) and atual['c'] > fundo_range and lateral:
        return {'tipo': 'spring', 'nivel': fundo_range, 'lateral': True, 'largura_pct': round(largura_pct * 100, 2)}

    if atual['h'] > topo_range * (1 + tolerancia_pct) and atual['c'] < topo_range and lateral:
        return {'tipo': 'utad', 'nivel': topo_range, 'lateral': True, 'largura_pct': round(largura_pct * 100, 2)}

    return None


HORAS_TOXICAS_UTC = {7, 23}  # troca de sessão / baixa liquidez, conforme especificado
GATES_COOLDOWN_SECONDS = 40 * 60  # dentro da faixa pedida de 30-60min
GATE_C_MONTE_CARLO_MIN_PROB = 85  # real, não decorativo — pode falhar com frequência
GATE_E_MIN_RR = 2.0
GATE_D_MIN_OBS = 3
GATE_D_MIN_FVGS = 2


def esta_em_hora_toxica_estrita(candles_referencia):
    """Horas tóxicas exatas do pipeline de Gates (07:00 e 23:00 UTC) —
    separado da lógica de killzone (bônus) já usada nos outros modos,
    porque aqui o requisito é BLOQUEAR, não só somar/subtrair pontos."""
    if not candles_referencia:
        return False
    dt = datetime.fromtimestamp(candles_referencia[-1]['t'] / 1000, tz=timezone.utc)
    return dt.hour in HORAS_TOXICAS_UTC


GATES_STALENESS_MAX_SEG = 90  # antes 30 — realista pro ciclo em lote (7-9 pares, várias chamadas cada)


def dados_obsoletos(candles, max_latencia_seg=GATES_STALENESS_MAX_SEG, intervalo_candle_seg=60):
    """
    Staleness — descarta a análise se o FECHAMENTO estimado do candle
    mais recente já tem mais de `max_latencia_seg` de idade.

    Correção importante: o timestamp de um candle é a ABERTURA, não o
    fechamento. Um candle M1 recém-aberto sempre tem 0-59s de "idade"
    mesmo em tempo real perfeito — medir direto da abertura reprovava
    quase todo ciclo à toa. Agora soma a duração do candle
    (`intervalo_candle_seg`) antes de comparar.
    """
    if not candles:
        return True
    fechamento_estimado = candles[-1]['t'] / 1000 + intervalo_candle_seg
    agora = time.time()
    return (agora - fechamento_estimado) > max_latencia_seg


def _confianca_padrao_candle(padrao, exec_candles):
    """Heurística de confiança do padrão de candle (Pin Bar/Hammer), com
    base na proporção pavio/corpo do último candle — NÃO é uma
    probabilidade estatisticamente validada, é um score relativo
    (quanto maior a rejeição, maior a 'confiança' do padrão)."""
    if not padrao or not exec_candles:
        return None
    c = exec_candles[-1]
    corpo = abs(c['c'] - c['o']) or 0.0001
    pavio_sup = c['h'] - max(c['o'], c['c'])
    pavio_inf = min(c['o'], c['c']) - c['l']
    razao = max(pavio_sup, pavio_inf) / corpo
    confianca = min(95, round(50 + razao * 10))
    return confianca


def init_gates_vortex_db(db_file):
    with sqlite3.connect(db_file) as conn:
        conn.execute('''
            CREATE TABLE IF NOT EXISTS scalp_gates_vortex_signal_state (
                id TEXT PRIMARY KEY,
                pair TEXT,
                created_at INTEGER,
                exec_tf TEXT,
                direcao TEXT,
                score INTEGER,
                entry REAL,
                sl REAL,
                tp1 REAL,
                tp2 REAL,
                resultado_final TEXT DEFAULT 'pendente',
                alerted INTEGER DEFAULT 0
            )
        ''')
        conn.commit()


def _save_gates_vortex_signal(db_file, pair, exec_tf_label, resultado, alerted):
    try:
        signal_id = f"gates_{pair}_{int(time.time()*1000)}"
        with sqlite3.connect(db_file) as conn:
            conn.execute('''
                INSERT INTO scalp_gates_vortex_signal_state
                    (id, pair, created_at, exec_tf, direcao, score, entry, sl, tp1, tp2, alerted)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ''', (
                signal_id, pair, int(time.time()), exec_tf_label,
                resultado['direcao'], resultado['score'],
                resultado['entry'], resultado['sl'], resultado['tp1'], resultado['tp2'],
                1 if alerted else 0,
            ))
            conn.commit()
    except Exception as e:
        print(f"[scalp_engine gates_vortex] erro ao salvar sinal de {pair}: {e}")


def process_pair_gates_vortex(db_file, pair, candles_por_tf, exec_tf_label='M1', send_telegram_fn=None):
    """
    Pipeline completo de Gates A-F. Só retorna SIGNAL_DISPARADO quando
    TODOS os 6 gates passarem. Reaproveita Bias/SFP/MSS/FVG do modo
    'sfp_liquidez' e as camadas de score do modo '4camadas' — não
    duplica lógica, só monta os gates por cima do que já existe.
    """
    resultado = {
        'pair': pair, 'exec_tf': exec_tf_label, 'modo': 'gates_vortex',
        'status': 'NEUTRAL', 'sinal': False, 'direcao': None,
        'score': 0, 'entry': None, 'sl': None, 'tp1': None, 'tp2': None,
        'gates': {}, 'motivo': None, 'em_cooldown': False,
    }

    candles_h1 = candles_por_tf.get('H1')
    candles_m15 = candles_por_tf.get('M15')
    candles_m5 = candles_por_tf.get('M5')
    candles_m1 = candles_por_tf.get('M1')
    candles_d1 = candles_por_tf.get('D1')

    candles_gatilho = candles_m1 or candles_m5
    intervalo_gatilho_seg = 60 if candles_m1 else 300
    if not candles_gatilho or not candles_h1:
        resultado['motivo'] = 'candles insuficientes (precisa pelo menos H1 e M1/M5)'
        return resultado

    # ── Staleness ──
    if dados_obsoletos(candles_gatilho, intervalo_candle_seg=intervalo_gatilho_seg):
        resultado['motivo'] = f'dados obsoletos — candle mais recente com mais de {GATES_STALENESS_MAX_SEG}s'
        return resultado

    # ── Hora tóxica ──
    if esta_em_hora_toxica_estrita(candles_gatilho):
        resultado['motivo'] = 'horário tóxico (troca de sessão/baixa liquidez) — sinal bloqueado'
        return resultado

    # ── Bias (Midnight Open) ──
    direcao_permitida, midnight_open, bias_context = compute_bias_midnight_open_estrito(pair, candles_por_tf)
    if direcao_permitida is None:
        resultado['motivo'] = 'sem candles suficientes pra calcular Midnight Open'
        return resultado

    # ── SFP contra liquidez (sessão anterior ou 3-7 dias) ──
    liquidez = compute_liquidez_referencia(pair, candles_por_tf)
    sfp, motivo_sfp, tf_sfp_usado = validar_sfp_cascata_tf(candles_por_tf, liquidez, direcao_permitida)
    if not sfp:
        resultado['motivo'] = f'Bias {bias_context}, {motivo_sfp}'
        return resultado
    resultado['tf_sfp_usado'] = tf_sfp_usado

    # ── Validação do gatilho de rejeição: padrão + volume + ATR múltiplo ──
    padrao = detect_candle_pattern(candles_gatilho)
    confianca_padrao = _confianca_padrao_candle(padrao, candles_gatilho)
    if not padrao or (confianca_padrao is not None and confianca_padrao < 75):
        resultado['motivo'] = f'SFP confirmado, mas padrão de rejeição fraco (confiança {confianca_padrao})'
        return resultado

    vols = [c.get('v', 0) for c in candles_gatilho[-20:]]
    media_vol = sum(vols) / len(vols) if vols else 0
    volume_ok = candles_gatilho[-1].get('v', 0) > media_vol * 1.0 if media_vol else False

    atr_atual = next((v for v in reversed(compute_atr(candles_gatilho, 14)) if v is not None), None)
    ultimo = candles_gatilho[-1]
    range_atual = ultimo['h'] - ultimo['l']
    atr_multiplo = round(range_atual / atr_atual, 2) if atr_atual and atr_atual > 0 else None
    atr_ok = atr_multiplo is not None and atr_multiplo > 1.2

    # ── MSS no M1 ──
    if not candles_m1:
        resultado['motivo'] = 'SFP e gatilho ok, mas sem candles M1 pra validar MSS'
        return resultado
    mss = validar_mss_m1(candles_m1, sfp, direcao_permitida)
    if not mss:
        resultado['motivo'] = f"SFP confirmado ({sfp['tipo']}), mas sem MSS de corpo forte no M1 ainda"
        return resultado

    # ── FVG (POI), entrada 50% ──
    entry_zone = find_fvg_ob_after_choch(mss['candles_ref'], mss)
    if not entry_zone or entry_zone.get('tipo') != 'FVG':
        resultado['motivo'] = 'MSS confirmado, mas sem FVG real após a expansão'
        return resultado
    entry = (entry_zone['top'] + entry_zone['bottom']) / 2
    sl = calcular_sl_estrito(pair, sfp)
    risco = abs(entry - sl)
    if risco <= 0:
        resultado['motivo'] = 'risco calculado inválido'
        return resultado

    # ── Camadas de score (reaproveita as do modo 4camadas) ──
    h4_candles = candles_por_tf.get('H4')
    h1_candles_regime = candles_h1
    pts_regime, det_regime = _camada_regime_mtf(candles_d1 or candles_h1, h4_candles, h1_candles_regime)
    pts_confluencias, det_confluencias = _camada_confluencias(candles_gatilho, direcao_permitida)

    mc = compute_monte_carlo(candles_gatilho)
    prob_acerto = None
    if mc:
        prob_acerto = mc['prob_alta_pct'] if direcao_permitida == 'alta' else mc['prob_baixa_pct']

    smc_quality = {'obs': len(find_order_blocks(candles_gatilho)), 'fvgs': len(find_open_fvgs(candles_gatilho))}

    tp1, _ = calcular_tp_dinamico(direcao_permitida, entry, sl, candles_m15 or candles_gatilho, candles_d1 or [], min_rr=GATE_E_MIN_RR)
    tp2, _ = calcular_tp_dinamico(direcao_permitida, entry, sl, candles_m15 or candles_gatilho, candles_d1 or [], min_rr=3.0)
    rr_tp1 = round(abs(tp1 - entry) / risco, 2) if tp1 else 0

    ichimoku = compute_ichimoku(candles_gatilho)
    ichi_bias = None
    if ichimoku and ichimoku.get('senkou_a') is not None:
        topo = max(ichimoku['senkou_a'], ichimoku['senkou_b'])
        fundo = min(ichimoku['senkou_a'], ichimoku['senkou_b'])
        preco = candles_gatilho[-1]['c']
        ichi_bias = 'BULLISH' if preco > topo else ('BEARISH' if preco < fundo else 'NEUTRAL')

    supertrend_linha, supertrend_dir = compute_supertrend(candles_h1, 10, 3.0)
    supertrend_atual = supertrend_dir[-1] if supertrend_dir else None

    wyckoff = detect_wyckoff_spring_utad(candles_gatilho)

    # ── GATES ──
    gate_a = (det_regime.get('bias_d1') == direcao_permitida or det_regime.get('mtf_alinhado')) and (mss['direcao'] == direcao_permitida)
    gate_b = bool(sfp) and bool(mss) and bool(entry_zone)
    gate_c = prob_acerto is not None and prob_acerto >= GATE_C_MONTE_CARLO_MIN_PROB
    gate_d = smc_quality['obs'] >= GATE_D_MIN_OBS and smc_quality['fvgs'] >= GATE_D_MIN_FVGS
    gate_e = rr_tp1 >= GATE_E_MIN_RR
    gate_f = True  # informativo, nunca bloqueia

    gates = {
        'A_MTF_ALIGNMENT': gate_a,
        'B_TRIGGER': gate_b,
        'C_MONTE_CARLO': gate_c,
        'D_SMC_QUALITY': gate_d,
        'E_MIN_RR': gate_e,
        'F_ICHIMOKU_INFO': gate_f,
    }
    resultado['gates'] = gates

    if not all(gates.values()):
        falhos = [nome for nome, ok in gates.items() if not ok]
        resultado['motivo'] = f"Pipeline chegou até o FVG, mas falhou nos gates: {', '.join(falhos)}"
        resultado['score'] = pts_regime + pts_confluencias
        return resultado

    score = min(100, pts_regime + pts_confluencias + (25 if volume_ok else 0) + (confianca_padrao or 0) // 4)

    volatilidade = None
    if atr_atual and entry:
        atr_pct = (atr_atual / entry) * 100
        volatilidade = 'LOW' if atr_pct < 0.3 else ('MEDIUM' if atr_pct < 0.8 else 'HIGH')

    resultado.update({
        'status': 'SIGNAL_DISPARADO', 'sinal': True, 'direcao': direcao_permitida,
        'score': score, 'prob_acerto': prob_acerto,
        'entry': round(entry, 6), 'sl': round(sl, 6),
        'tp1': round(tp1, 6) if tp1 else None, 'tp2': round(tp2, 6) if tp2 else None,
        'tp': round(tp1, 6) if tp1 else None,
        'risco_recompensa': f"1:{rr_tp1}",
        'regime': det_regime.get('regime'), 'adx': det_regime.get('adx'),
        'estrutura': 'CHOCH', 'gatilho': padrao, 'confianca_padrao': confianca_padrao,
        'volatilidade': volatilidade, 'vies_contexto': f"{det_regime.get('viés_contexto','NEUTRO')}_MACRO",
        'atr_multiplo': atr_multiplo, 'smc_quality': smc_quality,
        'ichimoku_bias': ichi_bias, 'supertrend_direcao': supertrend_atual,
        'wyckoff': wyckoff,
        'motivo': 'entrada_confirmada',
    })

    segundos_desde = _segundos_desde_ultimo_alerta(db_file, 'scalp_gates_vortex_signal_state', pair)
    em_cooldown = segundos_desde is not None and segundos_desde < GATES_COOLDOWN_SECONDS
    resultado['em_cooldown'] = em_cooldown

    _save_gates_vortex_signal(db_file, pair, exec_tf_label, resultado, alerted=not em_cooldown)

    if send_telegram_fn and not em_cooldown:
        arrow = '📈' if direcao_permitida == 'alta' else '📉'
        label = 'COMPRA' if direcao_permitida == 'alta' else 'VENDA'
        msg = f"🚦 <b>Gates Vortex — {pair}</b>\n\n"
        msg += f"{arrow} <b>{label}</b> | Score: {score}/100 | Prob. real: {prob_acerto}%\n"
        msg += f"📍 Entrada: {resultado['entry']}\n🛑 Stop: {resultado['sl']}\n"
        msg += f"✅ TP1: {resultado['tp1']} (RR {resultado['risco_recompensa']})\n"
        if resultado['tp2']:
            msg += f"✅ TP2: {resultado['tp2']}\n"
        msg += "\n<i>Todos os 6 gates confirmados (A-F).</i>"
        send_telegram_fn(msg)
    elif em_cooldown:
        restante_min = (GATES_COOLDOWN_SECONDS - segundos_desde) // 60
        resultado['motivo'] = f'entrada_confirmada, mas em cooldown ({restante_min}min restantes)'

    return resultado


def formatar_saida_gates_json(resultado):
    """Formato de saída EXATO pedido, no estilo do sistema de Gates."""
    if resultado.get('status') != 'SIGNAL_DISPARADO':
        return {'status': 'NEUTRAL', 'ativo': resultado.get('pair'), 'motivo': resultado.get('motivo'), 'gates': resultado.get('gates')}

    return {
        'status': 'SIGNAL_DISPARADO',
        'ativo': resultado.get('pair'),
        'sinal': 'COMPRA' if resultado.get('direcao') == 'alta' else 'VENDA',
        'score': resultado.get('score'),
        'prob_acerto': f"{resultado.get('prob_acerto')}%" if resultado.get('prob_acerto') is not None else None,
        'risco_retorno': resultado.get('risco_recompensa'),
        'execucao': {
            'entrada': resultado.get('entry'),
            'stop_loss': resultado.get('sl'),
            'take_profit_1': resultado.get('tp1'),
            'take_profit_2': resultado.get('tp2'),
        },
        'motivos_principais': {
            'regime': resultado.get('regime'),
            'estrutura': resultado.get('estrutura'),
            'gatilho': resultado.get('gatilho'),
            'tipo': 'CONTINUATION',
            'padrao': f"{resultado.get('gatilho')} ({resultado.get('confianca_padrao')}%)" if resultado.get('gatilho') else None,
        },
        'contexto_mercado': {
            'adx_14': resultado.get('adx'),
            'volatilidade': resultado.get('volatilidade'),
            'vies_contexto': resultado.get('vies_contexto'),
            'atr_multiplo': resultado.get('atr_multiplo'),
        },
        'gates_validacao': resultado.get('gates'),
    }







def _save_filtro_shadow(db_file, pair, exec_tf_label, direcao, score, entry, sl, tp, filtros_bloqueados):
    try:
        shadow_id = f"shadow_{pair}_{int(time.time()*1000)}"
        with sqlite3.connect(db_file) as conn:
            cursor = conn.cursor()
            cursor.execute('''
                INSERT INTO scalp_filtros_shadow (id, pair, created_at, exec_tf, direcao, score, entry, sl, tp, filtros_que_bloqueariam, resultado)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'pendente')
            ''', (
                shadow_id, pair, int(time.time()), exec_tf_label,
                direcao, score, entry, sl, tp, ','.join(filtros_bloqueados),
            ))
            conn.commit()
    except Exception as e:
        print(f"[scalp_engine] erro ao salvar filtro shadow de {pair}: {e}")


SHADOW_RESOLVE_MAX_AGE_HOURS = 24


def _resolver_filtros_shadow_pendentes(db_file, pair, exec_candles):
    try:
        with sqlite3.connect(db_file) as conn:
            cursor = conn.cursor()
            cursor.execute('''
                SELECT id, created_at, direcao, entry, sl, tp FROM scalp_filtros_shadow
                WHERE pair=? AND (resultado IS NULL OR resultado='pendente')
            ''', (pair,))
            pendentes = cursor.fetchall()

        if not pendentes:
            return

        now = int(time.time())
        for shadow_id, created_at, direcao, entry, sl, tp in pendentes:
            if sl is None or tp is None:
                continue
            candles_apos = [c for c in exec_candles if c['t'] >= created_at]
            resultado = None
            for c in candles_apos:
                if direcao == 'alta':
                    if c['l'] <= sl:
                        resultado = 'loss'
                        break
                    if c['h'] >= tp:
                        resultado = 'win'
                        break
                else:
                    if c['h'] >= sl:
                        resultado = 'loss'
                        break
                    if c['l'] <= tp:
                        resultado = 'win'
                        break

            if resultado is None and (now - created_at) > SHADOW_RESOLVE_MAX_AGE_HOURS * 3600:
                resultado = 'expirado'

            if resultado:
                try:
                    with sqlite3.connect(db_file) as conn:
                        conn.execute(
                            'UPDATE scalp_filtros_shadow SET resultado=? WHERE id=?',
                            (resultado, shadow_id)
                        )
                        conn.commit()
                except Exception as e:
                    print(f"[scalp_engine] erro ao resolver shadow {shadow_id}: {e}")
    except Exception as e:
        print(f"[scalp_engine] erro ao checar pendentes shadow de {pair}: {e}")




def filtros_shadow_report(db_file, pair=None, limit=50):
    try:
        with sqlite3.connect(db_file) as conn:
            cursor = conn.cursor()
            if pair:
                cursor.execute('''
                    SELECT pair, created_at, exec_tf, direcao, score, entry, sl, tp, filtros_que_bloqueariam, resultado
                    FROM scalp_filtros_shadow WHERE pair=? ORDER BY created_at DESC LIMIT ?
                ''', (pair, limit))
            else:
                cursor.execute('''
                    SELECT pair, created_at, exec_tf, direcao, score, entry, sl, tp, filtros_que_bloqueariam, resultado
                    FROM scalp_filtros_shadow ORDER BY created_at DESC LIMIT ?
                ''', (limit,))
            rows = cursor.fetchall()

        casos = []
        contagem = {}
        stats_por_filtro = {}
        for r in rows:
            filtros = r[8].split(',') if r[8] else []
            resultado = r[9] or 'pendente'
            for f in filtros:
                nome_base = f.split('(')[0]
                contagem[nome_base] = contagem.get(nome_base, 0) + 1
                if nome_base not in stats_por_filtro:
                    stats_por_filtro[nome_base] = {'win': 0, 'loss': 0, 'pendente': 0, 'expirado': 0}
                stats_por_filtro[nome_base][resultado] = stats_por_filtro[nome_base].get(resultado, 0) + 1
            casos.append({
                'pair': r[0], 'created_at': r[1], 'exec_tf': r[2], 'direcao': r[3],
                'score': r[4], 'entry': r[5], 'sl': r[6], 'tp': r[7],
                'filtros_que_bloqueariam': filtros, 'resultado': resultado,
            })

        for nome, s in stats_por_filtro.items():
            resolvidos = s['win'] + s['loss']
            s['resolvidos'] = resolvidos
            s['win_rate_pct'] = round(100 * s['win'] / resolvidos, 1) if resolvidos > 0 else None

        return {
            'total_casos': len(casos),
            'contagem_por_filtro': contagem,
            'win_rate_por_filtro': stats_por_filtro,
            'casos': casos,
        }
    except Exception as e:
        print(f"[scalp_engine] erro ao gerar filtros_shadow_report: {e}")
        return {'total_casos': 0, 'contagem_por_filtro': {}, 'casos': [], 'error': str(e)}


BE_PROGRESSO_PCT = 0.30
PARCIAL_PROGRESSO_PCT = 0.50


def _progresso_ate_tp(preco_atual, entry, sl, tp, direcao):
    dist_total = abs(tp - entry)
    if dist_total <= 0:
        return 0
    avanco = (preco_atual - entry) if direcao == 'alta' else (entry - preco_atual)
    return avanco / dist_total


def gerenciar_trades_abertos(db_file, pair, exec_candles, table, send_telegram_fn=None,
                              be_progresso_pct=BE_PROGRESSO_PCT, parcial_progresso_pct=PARCIAL_PROGRESSO_PCT):
    if not exec_candles:
        return
    preco_atual = exec_candles[-1]['c']

    try:
        with sqlite3.connect(db_file) as conn:
            cursor = conn.cursor()
            cursor.execute(f'''
                SELECT id, direcao, entry, sl, tp, be_movido, parcial_feita
                FROM {table}
                WHERE pair=? AND alerted=1 AND (resultado_final IS NULL OR resultado_final='pendente')
            ''', (pair,))
            trades = cursor.fetchall()
    except Exception as e:
        print(f"[scalp_engine] erro ao buscar trades abertos ({table}, {pair}): {e}")
        return

    if not trades:
        return

    for trade_id, direcao, entry, sl, tp, be_movido, parcial_feita in trades:
        if entry is None or sl is None or tp is None:
            continue

        resultado_final = None
        for c in exec_candles:
            if direcao == 'alta':
                if c['l'] <= sl:
                    resultado_final = 'loss'
                    break
                if c['h'] >= tp:
                    resultado_final = 'win'
                    break
            else:
                if c['h'] >= sl:
                    resultado_final = 'loss'
                    break
                if c['l'] <= tp:
                    resultado_final = 'win'
                    break

        if resultado_final:
            try:
                with sqlite3.connect(db_file) as conn:
                    conn.execute(f"UPDATE {table} SET resultado_final=? WHERE id=?", (resultado_final, trade_id))
                    conn.commit()
            except Exception as e:
                print(f"[scalp_engine] erro ao fechar trade {trade_id} ({table}): {e}")
            continue

        progresso = _progresso_ate_tp(preco_atual, entry, sl, tp, direcao)

        if progresso >= parcial_progresso_pct and not parcial_feita:
            dist_total = abs(tp - entry)
            pips_ganhos = round(dist_total * parcial_progresso_pct, 6)
            try:
                with sqlite3.connect(db_file) as conn:
                    conn.execute(
                        f"UPDATE {table} SET parcial_feita=1, be_movido=1, status_gestao=? WHERE id=?",
                        (f"PARCIAL +{pips_ganhos}", trade_id)
                    )
                    conn.commit()
            except Exception as e:
                print(f"[scalp_engine] erro ao marcar parcial do trade {trade_id} ({table}): {e}")
            if send_telegram_fn:
                arrow = '📈' if direcao == 'alta' else '📉'
                msg = f"🎯 <b>Realizar Parcial — {pair}</b>\n\n"
                msg += f"{arrow} Preço já andou {round(progresso*100)}% do caminho até o TP\n"
                msg += f"💰 Sugestão: realize 50-70% da posição agora e mova o resto do Stop pra entrada ({entry})\n"
                msg += f"📍 Entrada original: {entry} | Preço atual: {round(preco_atual, 6)}"
                send_telegram_fn(msg)
        elif progresso >= be_progresso_pct and not be_movido:
            try:
                with sqlite3.connect(db_file) as conn:
                    conn.execute(
                        f"UPDATE {table} SET be_movido=1, status_gestao=? WHERE id=?",
                        ("BE", trade_id)
                    )
                    conn.commit()
            except Exception as e:
                print(f"[scalp_engine] erro ao marcar BE do trade {trade_id} ({table}): {e}")
            if send_telegram_fn:
                arrow = '📈' if direcao == 'alta' else '📉'
                msg = f"🛡️ <b>Mover Stop para Break Even — {pair}</b>\n\n"
                msg += f"{arrow} Preço já andou {round(progresso*100)}% do caminho até o TP\n"
                msg += f"💡 Sugestão: mova o Stop pra entrada ({entry}) — trava o risco em zero, deixa o resto correr\n"
                msg += f"📍 Preço atual: {round(preco_atual, 6)}"
                send_telegram_fn(msg)
def _save_rapido_signal(db_file, pair, exec_tf_label, resultado, alerted):
    try:
        signal_id = f"rapido_{pair}_{int(time.time()*1000)}"
        with sqlite3.connect(db_file) as conn:
            cursor = conn.cursor()
            cursor.execute('''
                INSERT INTO scalp_rapido_signal_state (id, pair, created_at, exec_tf, direcao, entry, sl, tp, zona_tipo, alerted)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ''', (
                signal_id, pair, int(time.time()), exec_tf_label,
                resultado['direcao'], resultado['entry'], resultado['sl'], resultado['tp'],
                resultado.get('zona_tipo'), 1 if alerted else 0,
            ))
            conn.commit()
    except Exception as e:
        print(f"[scalp_engine] erro ao salvar signal rápido de {pair}: {e}")


def detect_sweep_zona_diaria_movel(exec_candles, zona_diaria, lookback=10):
    resistencia = zona_diaria['resistencia']
    suporte = zona_diaria['suporte']
    sweep_resistencia = None
    sweep_suporte = None

    for i in range(len(exec_candles) - 1, max(0, len(exec_candles) - lookback), -1):
        c = exec_candles[i]
        if sweep_resistencia is None and c['h'] > resistencia['top'] and c['c'] < resistencia['top']:
            sweep_resistencia = {'index': i, 'lado': 'alta', 'nivel': c['h'], 't': c['t'], 'tipo_zona': 'resistencia'}
        if sweep_suporte is None and c['l'] < suporte['bottom'] and c['c'] > suporte['bottom']:
            sweep_suporte = {'index': i, 'lado': 'baixa', 'nivel': c['l'], 't': c['t'], 'tipo_zona': 'suporte'}
        if sweep_resistencia and sweep_suporte:
            break

    candidatos = [s for s in (sweep_resistencia, sweep_suporte) if s]
    if not candidatos:
        return None
    return max(candidatos, key=lambda s: s['t'])




CASCATA_COOLDOWN_SECONDS = 30 * 60


def _save_cascata_signal(db_file, pair, exec_tf_label, resultado, alerted):
    try:
        signal_id = f"cascata_{pair}_{int(time.time()*1000)}"
        with sqlite3.connect(db_file) as conn:
            cursor = conn.cursor()
            cursor.execute('''
                INSERT INTO scalp_cascata_signal_state
                    (id, pair, created_at, exec_tf, direcao, entry, sl, tp,
                     bias_semanal, bias_d1, bias_h4, bias_h1, evento_tipo, alerted)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ''', (
                signal_id, pair, int(time.time()), exec_tf_label,
                resultado['direcao'], resultado['entry'], resultado['sl'], resultado['tp'],
                resultado.get('bias_semanal'), resultado.get('bias_d1'),
                resultado.get('bias_h4'), resultado.get('bias_h1'),
                resultado.get('evento_tipo'), 1 if alerted else 0,
            ))
            conn.commit()
    except Exception as e:
        print(f"[scalp_engine] erro ao salvar signal cascata de {pair}: {e}")




RSI_EXTREMO_BAIXA = 20
RSI_EXTREMO_ALTA = 80
LIQUIDEZ_LOOKBACK = 40
RR_FIXO_ANTECIPADO = 2.0
ANTECIPADO_SWEEP_LOOKBACK = 10


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




VOTOS_MINIMOS_SINAL = 8
ATR_MULT_STOP = 1.5
RR_INDICADORES = 2.0

VWAP_POC_MIN_DIST_PCT = 0.003
EMA_SLOPE_MIN_PCT = 0.001
MONTE_CARLO_GATE_VOTE_MIN_PROB = 62


def _votos_indicadores(indicadores, preco_atual):
    votos = []

    macd_hist = indicadores.get('macd_hist')
    if macd_hist is not None:
        votos.append(('macd', 'alta' if macd_hist > 0 else 'baixa'))

    ema9, ema21, ema50 = indicadores.get('ema9'), indicadores.get('ema21'), indicadores.get('ema50')
    ema9_slope = indicadores.get('ema9_slope')
    if ema9 is not None and ema21 is not None and ema50 is not None and ema9_slope is not None:
        slope_min = preco_atual * EMA_SLOPE_MIN_PCT
        if ema9 > ema21 > ema50 and ema9_slope > slope_min:
            votos.append(('emas', 'alta'))
        elif ema9 < ema21 < ema50 and ema9_slope < -slope_min:
            votos.append(('emas', 'baixa'))

    rsi = indicadores.get('rsi14')
    if rsi is not None:
        if rsi <= 35:
            votos.append(('rsi', 'alta'))
        elif rsi >= 65:
            votos.append(('rsi', 'baixa'))

    stoch_k, stoch_d = indicadores.get('stoch_k'), indicadores.get('stoch_d')
    if stoch_k is not None and stoch_d is not None:
        if stoch_k <= 30 and stoch_d <= 30:
            votos.append(('stochastic', 'alta'))
        elif stoch_k >= 70 and stoch_d >= 70:
            votos.append(('stochastic', 'baixa'))

    bb_lower, bb_upper = indicadores.get('bollinger_lower'), indicadores.get('bollinger_upper')
    if bb_lower is not None and bb_upper is not None:
        if preco_atual <= bb_lower:
            votos.append(('bollinger', 'alta'))
        elif preco_atual >= bb_upper:
            votos.append(('bollinger', 'baixa'))

    vwap = indicadores.get('vwap')
    if vwap is not None and vwap > 0:
        dist_pct = abs(preco_atual - vwap) / vwap
        if dist_pct >= VWAP_POC_MIN_DIST_PCT:
            votos.append(('vwap', 'alta' if preco_atual > vwap else 'baixa'))

    poc = indicadores.get('volume_profile_poc')
    if poc is not None and poc > 0:
        dist_pct = abs(preco_atual - poc) / poc
        if dist_pct >= VWAP_POC_MIN_DIST_PCT:
            votos.append(('volume_profile_poc', 'alta' if preco_atual > poc else 'baixa'))

    ichimoku = indicadores.get('ichimoku') or {}
    senkou_a, senkou_b = ichimoku.get('senkou_a'), ichimoku.get('senkou_b')
    if senkou_a is not None and senkou_b is not None:
        topo_nuvem, fundo_nuvem = max(senkou_a, senkou_b), min(senkou_a, senkou_b)
        if preco_atual > topo_nuvem:
            votos.append(('ichimoku', 'alta'))
        elif preco_atual < fundo_nuvem:
            votos.append(('ichimoku', 'baixa'))

    mc = indicadores.get('monte_carlo') or {}
    prob_alta, prob_baixa = mc.get('prob_alta_pct'), mc.get('prob_baixa_pct')
    if prob_alta is not None and prob_baixa is not None:
        if prob_alta >= MONTE_CARLO_GATE_VOTE_MIN_PROB:
            votos.append(('monte_carlo', 'alta'))
        elif prob_baixa >= MONTE_CARLO_GATE_VOTE_MIN_PROB:
            votos.append(('monte_carlo', 'baixa'))

    padrao = indicadores.get('candle_pattern')
    if padrao in ('Engolfo de Alta', 'Martelo (Hammer)'):
        votos.append(('candle_pattern', 'alta'))
    elif padrao in ('Engolfo de Baixa', 'Estrela Cadente (Shooting Star)'):
        votos.append(('candle_pattern', 'baixa'))

    adx = indicadores.get('adx14')
    tendencia_forte = adx is not None and adx >= 25

    votos_alta = sum(1 for _, v in votos if v == 'alta')
    votos_baixa = sum(1 for _, v in votos if v == 'baixa')
    return votos_alta, votos_baixa, len(votos), votos, tendencia_forte


def _stop_via_ultimo_swing(exec_candles, direcao, lookback=SWING_LOOKBACK):
    swings = detect_exec_swings(exec_candles, lookback=lookback)
    if direcao == 'alta':
        lows = [s for s in swings if s['tipo'] == 'low']
        if lows:
            return lows[-1]['valor']
    else:
        highs = [s for s in swings if s['tipo'] == 'high']
        if highs:
            return highs[-1]['valor']
    return None




MODOS_SCALP = {
    'normal_choch': 'scalp_signal_state',
    'continuacao_bos': 'scalp_signal_state_continuacao',
    'antecipado_v2': 'scalp_antecipado_signal_state',
    'confluencia_indicadores': 'scalp_indicadores_signal_state',
    'scalp_rapido': 'scalp_rapido_signal_state',
    'cascata_smc': 'scalp_cascata_signal_state',
    '4camadas': 'scalp_4camadas_signal_state',
    'sfp_liquidez': 'scalp_sfp_liquidez_signal_state',
    'gates_vortex': 'scalp_gates_vortex_signal_state',
}

_TABELAS_COM_SCORE = {
    'scalp_signal_state', 'scalp_signal_state_continuacao', 'scalp_indicadores_signal_state'
}

_TABELAS_COM_MOTIVO = {
    'scalp_signal_state', 'scalp_signal_state_continuacao', 'scalp_indicadores_signal_state'
}

_COLUNAS_POR_TABELA = {
    'scalp_signal_state':
        "id, pair, created_at, exec_tf, direcao, score, entry, sl, tp, na_killzone, resultado_final, motivo_score",
    'scalp_signal_state_continuacao':
        "id, pair, created_at, exec_tf, direcao, score, entry, sl, tp, na_killzone, resultado_final, motivo_score",
    'scalp_antecipado_signal_state':
        "id, pair, created_at, exec_tf, direcao, rsi, liquidez_varrida, divergencia_rsi, entry, sl, tp, resultado_final",
    'scalp_indicadores_signal_state':
        "id, pair, created_at, exec_tf, direcao, score, votos_favor, votos_total, entry, sl, tp, resultado_final, motivo_score",
    'scalp_rapido_signal_state':
        "id, pair, created_at, exec_tf, direcao, entry, sl, tp, zona_tipo, resultado_final",
    'scalp_cascata_signal_state':
        "id, pair, created_at, exec_tf, direcao, entry, sl, tp, bias_semanal, bias_d1, bias_h4, bias_h1, evento_tipo, resultado_final",
    'scalp_4camadas_signal_state':
        "id, pair, created_at, exec_tf, direcao, score, entry, sl, tp, resultado_final",
}


def _stats_de_uma_tabela(db_file, tabela):
    try:
        with sqlite3.connect(db_file) as conn:
            cursor = conn.cursor()
            cursor.execute(f'''
                SELECT resultado_final, COUNT(*), direcao
                FROM {tabela}
                WHERE alerted=1
                GROUP BY resultado_final, direcao
            ''')
            rows = cursor.fetchall()
    except Exception as e:
        return {'erro': str(e), 'wins': 0, 'losses': 0, 'pendentes': 0}

    wins = losses = pendentes = 0
    wins_long = wins_short = losses_long = losses_short = 0
    for resultado, count, direcao in rows:
        if resultado == 'win':
            wins += count
            if direcao == 'alta':
                wins_long += count
            else:
                wins_short += count
        elif resultado == 'loss':
            losses += count
            if direcao == 'alta':
                losses_long += count
            else:
                losses_short += count
        else:
            pendentes += count

    total_resolvidos = wins + losses
    win_rate = round(100 * wins / total_resolvidos, 1) if total_resolvidos > 0 else None

    total_long = wins_long + losses_long
    total_short = wins_short + losses_short
    win_rate_long = round(100 * wins_long / total_long, 1) if total_long > 0 else None
    win_rate_short = round(100 * wins_short / total_short, 1) if total_short > 0 else None

    return {
        'wins': wins, 'losses': losses, 'pendentes': pendentes,
        'total_resolvidos': total_resolvidos, 'win_rate_pct': win_rate,
        'long': {'wins': wins_long, 'losses': losses_long, 'win_rate_pct': win_rate_long},
        'short': {'wins': wins_short, 'losses': losses_short, 'win_rate_pct': win_rate_short},
    }


def _stats_por_score_bracket(db_file, tabela):
    if tabela not in _TABELAS_COM_SCORE:
        return None

    try:
        with sqlite3.connect(db_file) as conn:
            cursor = conn.cursor()
            cursor.execute(f'''
                SELECT score, resultado_final FROM {tabela}
                WHERE alerted=1 AND resultado_final IN ('win','loss')
            ''')
            rows = cursor.fetchall()
    except Exception as e:
        return {'erro': str(e)}

    brackets = {'50-59': {'w': 0, 'l': 0}, '60-74': {'w': 0, 'l': 0}, '75-100': {'w': 0, 'l': 0}}
    for score, resultado in rows:
        if score is None:
            continue
        if 50 <= score <= 59:
            k = '50-59'
        elif 60 <= score <= 74:
            k = '60-74'
        elif score >= 75:
            k = '75-100'
        else:
            continue
        if resultado == 'win':
            brackets[k]['w'] += 1
        else:
            brackets[k]['l'] += 1

    for k, v in brackets.items():
        total = v['w'] + v['l']
        v['win_rate_pct'] = round(100 * v['w'] / total, 1) if total > 0 else None
        v['total'] = total

    return brackets


def _stats_por_componente_motivo(db_file, tabela, top_n=15):
    if tabela not in _TABELAS_COM_MOTIVO:
        return None

    try:
        with sqlite3.connect(db_file) as conn:
            cursor = conn.cursor()
            cursor.execute(f'''
                SELECT motivo_score, resultado_final FROM {tabela}
                WHERE alerted=1 AND resultado_final IN ('win','loss') AND motivo_score IS NOT NULL AND motivo_score != ''
            ''')
            rows = cursor.fetchall()
    except Exception as e:
        return {'erro': str(e)}

    if not rows:
        return {'amostras': 0, 'nota': 'sem sinais com motivo_score preenchido ainda (só sinais gravados após o patch têm isso)'}

    componentes = {}
    for motivo_texto, resultado in rows:
        for par_nome_pts in motivo_texto.split(','):
            if ':' not in par_nome_pts:
                continue
            nome = par_nome_pts.split(':')[0]
            if nome not in componentes:
                componentes[nome] = {'wins': 0, 'losses': 0}
            if resultado == 'win':
                componentes[nome]['wins'] += 1
            else:
                componentes[nome]['losses'] += 1

    lista = []
    for nome, v in componentes.items():
        total = v['wins'] + v['losses']
        lista.append({
            'componente': nome, 'wins': v['wins'], 'losses': v['losses'], 'total': total,
            'win_rate_pct': round(100 * v['wins'] / total, 1) if total > 0 else None,
        })
    lista.sort(key=lambda x: x['total'], reverse=True)

    return {'amostras': len(rows), 'componentes': lista[:top_n]}


def gerar_stats_por_modo(db_file):
    relatorio = {}
    for nome_modo, tabela in MODOS_SCALP.items():
        stats = _stats_de_uma_tabela(db_file, tabela)
        stats['por_score'] = _stats_por_score_bracket(db_file, tabela)
        stats['por_componente_motivo'] = _stats_por_componente_motivo(db_file, tabela)
        relatorio[nome_modo] = stats

    ranking = sorted(
        [
            (nome, dados['win_rate_pct'], dados['total_resolvidos'])
            for nome, dados in relatorio.items()
            if dados.get('win_rate_pct') is not None and dados.get('total_resolvidos', 0) >= 5
        ],
        key=lambda x: x[1], reverse=True,
    )

    return {
        'por_modo': relatorio,
        'ranking_win_rate': [
            {'modo': nome, 'win_rate_pct': wr, 'amostras': n} for nome, wr, n in ranking
        ],
    }


def listar_trades_detalhado(db_file, modo, limit=200):
    tabela = MODOS_SCALP.get(modo)
    if not tabela:
        return {'erro': f'modo desconhecido: {modo}. Use um de {list(MODOS_SCALP.keys())}'}

    cols = _COLUNAS_POR_TABELA[tabela]
    try:
        with sqlite3.connect(db_file) as conn:
            cursor = conn.cursor()
            cursor.execute(f'''
                SELECT {cols} FROM {tabela}
                WHERE alerted=1
                ORDER BY created_at DESC LIMIT ?
            ''', (limit,))
            rows = cursor.fetchall()
            nomes_col = [c.strip() for c in cols.split(',')]
    except Exception as e:
        return {'erro': str(e)}

    trades = [dict(zip(nomes_col, row)) for row in rows]
    return {'modo': modo, 'tabela': tabela, 'total': len(trades), 'trades': trades}


# ═══════════════════════════════════════════════════════════════════════
# MODO "4 CAMADAS" — réplica da lógica do app concorrente (Trading IA 24/7
# / Vortex), reconstruída a partir dos prints: Regime&MTF (30) + Estrutura
# SMC (30) + Gatilho&Energia (25) + Confluências (15) = 100.
#
# ATENÇÃO — decisão explícita do usuário: SEM gate de contradição. A
# direção final vem só da camada Gatilho&Energia (o micro trigger), e as
# outras 3 camadas só somam pontos, mesmo que apontem pra direção
# oposta. Isso é DE PROPÓSITO igual ao app original — inclusive reproduz
# o mesmo furo que gerou o sinal de COMPRA em XAUUSD com contexto
# BEARISH/Baixista/Premium que a gente identificou. Ver conversa: se
# quiser blindar isso depois, é só adicionar um gate comparando
# camada_regime['bias'] / camada_estrutura['direcao'] vs
# camada_gatilho['direcao'] antes de disparar o sinal.
# ═══════════════════════════════════════════════════════════════════════

SCORE_THRESHOLD_4CAMADAS = 75
COOLDOWN_4CAMADAS_SECONDS = 30 * 60

CANDLE_PATTERNS_BULLISH = ('Martelo (Hammer)', 'Engolfo de Alta')
CANDLE_PATTERNS_BEARISH = ('Estrela Cadente (Shooting Star)', 'Engolfo de Baixa')


def init_4camadas_db(db_file):
    with sqlite3.connect(db_file) as conn:
        conn.execute('''
            CREATE TABLE IF NOT EXISTS scalp_4camadas_signal_state (
                id TEXT PRIMARY KEY,
                pair TEXT,
                created_at INTEGER,
                exec_tf TEXT,
                direcao TEXT,
                score INTEGER,
                entry REAL,
                sl REAL,
                tp REAL,
                resultado_final TEXT DEFAULT 'pendente',
                alerted INTEGER DEFAULT 0
            )
        ''')
        conn.commit()


def _camada_regime_mtf(d1_candles, h4_candles, h1_candles):
    """Regime&MTF — até 30 pontos. Não decide direção, só soma."""
    regime, adx = compute_market_regime(d1_candles)
    pts = 0
    detalhes = {'regime': regime.upper() if regime else None, 'adx': adx}
    if regime == 'trending':
        pts += 15

    bias_d1 = compute_bias_from_swings(d1_candles)
    bias_h4 = compute_bias_from_swings(h4_candles) if h4_candles else 'neutro'
    bias_h1 = compute_bias_from_swings(h1_candles) if h1_candles else 'neutro'
    detalhes['bias_d1'] = bias_d1
    detalhes['bias_h4'] = bias_h4
    detalhes['bias_h1'] = bias_h1

    biases_validos = [b for b in (bias_d1, bias_h4, bias_h1) if b != 'neutro']
    mtf_alinhado = False
    if biases_validos and len(set(biases_validos)) == 1 and len(biases_validos) >= 2:
        pts += 15
        mtf_alinhado = True
    elif biases_validos:
        # concordância parcial (pelo menos 2 de 3 no mesmo lado)
        from collections import Counter
        contagem = Counter(biases_validos)
        if contagem.most_common(1)[0][1] >= 2:
            pts += 8

    detalhes['mtf_alinhado'] = mtf_alinhado
    detalhes['viés_contexto'] = (bias_d1 if bias_d1 != 'neutro' else bias_h4).upper() if (bias_d1 != 'neutro' or bias_h4 != 'neutro') else 'NEUTRO'
    return min(pts, 30), detalhes


def _camada_estrutura_smc(d1_candles, exec_candles):
    """Estrutura SMC — até 30 pontos. Também não decide direção final."""
    pts = 0
    detalhes = {'estrutura': None, 'zona_pd': None, 'choch_direcao': None}

    bandas = compute_d1_zones(d1_candles)
    preco_atual = exec_candles[-1]['c']
    zona = find_active_zone(bandas, preco_atual)
    if zona:
        pts += 10

    choch_direcao = None
    if zona:
        sweep = detect_sweep_in_zone(exec_candles, zona)
        if sweep:
            choch = detect_choch_after_sweep(exec_candles, sweep)
            if choch:
                pts += 10
                choch_direcao = choch['direcao']
                detalhes['choch_direcao'] = choch_direcao
                detalhes['estrutura'] = 'Baixista' if choch_direcao == 'baixa' else 'Altista'

                entry_zone = find_fvg_ob_after_choch(exec_candles, choch)
                if not entry_zone:
                    entry_zone = find_ifvg_after_choch(exec_candles, choch)
                if entry_zone:
                    pts += 10
                    detalhes['entry_zone_tipo'] = entry_zone['tipo']

    pd_zone = compute_premium_discount(exec_candles)
    if pd_zone:
        detalhes['zona_pd'] = 'PREMIUM' if preco_atual > pd_zone['equilibrium'] else 'DISCOUNT'

    return min(pts, 30), detalhes


def _camada_gatilho_energia(exec_candles):
    """Gatilho&Energia — até 25 pontos. ESSA camada decide a direção
    final do sinal (igual ao app original)."""
    pts = 0
    detalhes = {'padrao_candle': None, 'direcao': None, 'micro_bos': False, 'volume_acima_media': False}

    padrao = detect_candle_pattern(exec_candles)
    detalhes['padrao_candle'] = padrao

    direcao = None
    if padrao in CANDLE_PATTERNS_BULLISH:
        direcao = 'alta'
        pts += 10
    elif padrao in CANDLE_PATTERNS_BEARISH:
        direcao = 'baixa'
        pts += 10
    else:
        # fallback: direção da última vela, sem padrão de rejeição claro
        ultimo = exec_candles[-1]
        direcao = 'alta' if ultimo['c'] >= ultimo['o'] else 'baixa'

    detalhes['direcao'] = direcao

    micro = detect_micro_bos(exec_candles, direcao)
    if micro.get('confirmado'):
        pts += 10
        detalhes['micro_bos'] = True

    vols = [c.get('v', 0) for c in exec_candles[-20:]]
    if vols:
        media_vol = sum(vols) / len(vols)
        if exec_candles[-1].get('v', 0) > media_vol * 1.3:
            pts += 5
            detalhes['volume_acima_media'] = True

    return min(pts, 25), detalhes, direcao


def _camada_confluencias(exec_candles, direcao):
    """Confluências — até 15 pontos. Monte Carlo real + Ichimoku."""
    pts = 0
    detalhes = {'monte_carlo_ok': False, 'ichimoku_alinhado': False}

    mc = compute_monte_carlo(exec_candles)
    if mc:
        prob_favoravel = mc['prob_alta_pct'] if direcao == 'alta' else mc['prob_baixa_pct']
        if prob_favoravel >= 55:
            pts += 8
            detalhes['monte_carlo_ok'] = True
        detalhes['monte_carlo'] = mc

    ichi = compute_ichimoku(exec_candles)
    if ichi and ichi.get('senkou_a') is not None and ichi.get('senkou_b') is not None:
        topo = max(ichi['senkou_a'], ichi['senkou_b'])
        fundo = min(ichi['senkou_a'], ichi['senkou_b'])
        preco = exec_candles[-1]['c']
        ichi_bias = 'alta' if preco > topo else ('baixa' if preco < fundo else 'neutro')
        if ichi_bias == direcao:
            pts += 7
            detalhes['ichimoku_alinhado'] = True
        detalhes['ichimoku_bias'] = ichi_bias

    return min(pts, 15), detalhes


def _save_4camadas_signal(db_file, pair, exec_tf_label, resultado, alerted):
    try:
        signal_id = f"4cam_{pair}_{int(time.time()*1000)}"
        with sqlite3.connect(db_file) as conn:
            conn.execute('''
                INSERT INTO scalp_4camadas_signal_state
                    (id, pair, created_at, exec_tf, direcao, score, entry, sl, tp, alerted)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ''', (
                signal_id, pair, int(time.time()), exec_tf_label,
                resultado['direcao'], resultado['score'],
                resultado['entry'], resultado['sl'], resultado['tp'],
                1 if alerted else 0,
            ))
            conn.commit()
    except Exception as e:
        print(f"[scalp_engine 4camadas] erro ao salvar sinal de {pair}: {e}")


def process_pair_4camadas(db_file, pair, d1_candles, h4_candles, h1_candles, exec_candles,
                           exec_tf_label, send_telegram_fn=None):
    """
    Modo '4 Camadas' — réplica intencional da lógica do app concorrente,
    SEM gate de contradição entre camadas (decisão explícita do usuário).
    A direção final vem só da camada Gatilho&Energia.
    """
    resultado = {
        'pair': pair, 'exec_tf': exec_tf_label, 'modo': '4camadas',
        'sinal': False, 'direcao': None, 'entry': None, 'sl': None, 'tp': None,
        'score': 0, 'motivo': None, 'em_cooldown': False,
        'camada_regime': None, 'camada_estrutura': None,
        'camada_gatilho': None, 'camada_confluencias': None,
        'indicadores': None, 'gates': [],
    }

    try:
        resultado['indicadores'] = compute_technical_indicators(exec_candles)
    except Exception as e:
        print(f"[scalp_engine 4camadas] erro ao calcular indicadores de {pair}: {e}")

    pts_regime, det_regime = _camada_regime_mtf(d1_candles, h4_candles, h1_candles)
    pts_estrutura, det_estrutura = _camada_estrutura_smc(d1_candles, exec_candles)
    pts_gatilho, det_gatilho, direcao_final = _camada_gatilho_energia(exec_candles)
    pts_confluencias, det_confluencias = _camada_confluencias(exec_candles, direcao_final)

    score_total = pts_regime + pts_estrutura + pts_gatilho + pts_confluencias

    resultado['camada_regime'] = {**det_regime, 'score': pts_regime}
    resultado['camada_estrutura'] = {**det_estrutura, 'score': pts_estrutura}
    resultado['camada_gatilho'] = {**det_gatilho, 'score': pts_gatilho}
    resultado['camada_confluencias'] = {**det_confluencias, 'score': pts_confluencias}
    resultado['score'] = score_total
    resultado['direcao'] = direcao_final
    resultado['detalhes'] = [
        ('regime_mtf', pts_regime), ('estrutura_smc', pts_estrutura),
        ('gatilho_energia', pts_gatilho), ('confluencias', pts_confluencias),
    ]

    if score_total < SCORE_THRESHOLD_4CAMADAS:
        resultado['motivo'] = f'score {score_total} abaixo de {SCORE_THRESHOLD_4CAMADAS} — sem entrada'
        return resultado

    preco_atual = exec_candles[-1]['c']
    atr_series = compute_atr(exec_candles, 14)
    atr_atual = next((v for v in reversed(atr_series) if v is not None), None)
    if not atr_atual or atr_atual <= 0:
        resultado['motivo'] = 'score suficiente, mas ATR indisponível pra calcular stop'
        return resultado

    entry = preco_atual
    sl = entry - atr_atual * 1.5 if direcao_final == 'alta' else entry + atr_atual * 1.5
    risco = abs(entry - sl)
    tp = entry + risco * 2.5 if direcao_final == 'alta' else entry - risco * 2.5

    resultado.update({
        'sinal': True,
        'entry': round(entry, 6), 'sl': round(sl, 6), 'tp': round(tp, 6),
        'motivo': 'entrada_confirmada',
    })

    segundos_desde = _segundos_desde_ultimo_alerta(db_file, 'scalp_4camadas_signal_state', pair)
    em_cooldown = segundos_desde is not None and segundos_desde < COOLDOWN_4CAMADAS_SECONDS
    resultado['em_cooldown'] = em_cooldown

    _save_4camadas_signal(db_file, pair, exec_tf_label, resultado, alerted=not em_cooldown)

    if send_telegram_fn and not em_cooldown:
        arrow = '📈' if direcao_final == 'alta' else '📉'
        label = 'COMPRA' if direcao_final == 'alta' else 'VENDA'
        msg = f"🔶 <b>Sinal 4 Camadas — {pair}</b>\n\n"
        msg += f"{arrow} <b>{label}</b> | TF execução: {exec_tf_label}\n"
        msg += f"📍 Entrada: {resultado['entry']}\n🛑 Stop: {resultado['sl']}\n✅ TP: {resultado['tp']}\n\n"
        msg += f"<b>Score Total: {score_total}/100</b>\n"
        msg += f"• Regime&MTF: {pts_regime}/30 (viés contexto: {det_regime.get('viés_contexto')})\n"
        msg += f"• Estrutura SMC: {pts_estrutura}/30 (estrutura: {det_estrutura.get('estrutura')}, zona: {det_estrutura.get('zona_pd')})\n"
        msg += f"• Gatilho&Energia: {pts_gatilho}/25 (padrão: {det_gatilho.get('padrao_candle')})\n"
        msg += f"• Confluências: {pts_confluencias}/15\n\n"
        msg += "⚠️ <i>Modo experimental — réplica intencional da lógica do concorrente, sem gate de contradição entre camadas.</i>"
        send_telegram_fn(msg)
    elif em_cooldown:
        restante_min = (COOLDOWN_4CAMADAS_SECONDS - segundos_desde) // 60
        resultado['motivo'] = f'entrada_confirmada, mas em cooldown ({restante_min}min restantes)'

    return resultado


# Gera e persiste o payload completo (gates, monte carlo, ichimoku,
# breakdown de score) toda vez que um sinal de verdade sai, e expõe via
# endpoint Flask. Não modifica nenhuma função existente acima — só
# envolve (wrapper) as 6 funções process_pair_scalp_*.
# ═══════════════════════════════════════════════════════════════════════

TABELA_POR_MODO = MODOS_SCALP


def init_explicacao_db(db_file):
    with sqlite3.connect(db_file) as conn:
        conn.execute('''
            CREATE TABLE IF NOT EXISTS scalp_explicacoes (
                signal_id TEXT PRIMARY KEY,
                pair TEXT,
                modo TEXT,
                created_at INTEGER,
                payload_json TEXT
            )
        ''')
        conn.commit()


def _gates_para_filtros(gates):
    return [
        {'nome': g['nome'], 'passou': g['passou'], 'descricao': g.get('detalhe', '')}
        for g in (gates or [])
    ]


def build_explicacao_payload(resultado, modo, regime_info=None, exec_candles=None):
    direcao = resultado.get('direcao')
    entry, sl, tp = resultado.get('entry'), resultado.get('sl'), resultado.get('tp')
    rr = None
    if entry and sl and tp:
        risco = abs(entry - sl)
        rr = round(abs(tp - entry) / risco, 2) if risco else None

    indicadores = resultado.get('indicadores') or {}
    monte_carlo = indicadores.get('monte_carlo')
    ichimoku = indicadores.get('ichimoku')

    # ── ATR múltiplo (distância do stop em unidades de ATR) e
    # Volatilidade (ATR% do preço, bucket LOW/MEDIUM/HIGH) — dados reais,
    # calculados a partir do ATR que o engine já computa sempre.
    atr14 = indicadores.get('atr14')
    atr_multiplo = None
    if atr14 and entry and sl and atr14 > 0:
        atr_multiplo = round(abs(entry - sl) / atr14, 2)

    volatilidade = None
    if atr14 and entry and entry > 0:
        atr_pct = (atr14 / entry) * 100
        if atr_pct < 0.3:
            volatilidade = 'LOW'
        elif atr_pct < 0.8:
            volatilidade = 'MEDIUM'
        else:
            volatilidade = 'HIGH'

    # ── Vela de Rejeição / Energia confirmada — derivado do padrão de
    # candle que o engine já detecta (detect_candle_pattern) e do
    # componente de volume que já entra no compute_score.
    padrao_candle = indicadores.get('candle_pattern')
    vela_rejeicao = padrao_candle in (
        'Martelo (Hammer)', 'Estrela Cadente (Shooting Star)',
        'Engolfo de Alta', 'Engolfo de Baixa',
    )
    breakdown_bruto = resultado.get('detalhes') or resultado.get('votos_detalhe') or []
    energia_confirmada = bool(
        any(item[0] == 'volume_choch_forte' for item in breakdown_bruto)
        or resultado.get('tendencia_forte')
    )

    # ── Zona Premium/Discount — só existe pro modo cascata_smc, que já
    # calcula isso internamente (compute_premium_discount). Vem pronto
    # em resultado['zona_pd'] quando o process_pair_cascata_smc grava.
    zona_pd = resultado.get('zona_pd')

    # ── SMC Quality (nº de Order Blocks e FVGs abertos) — só calculável
    # se os candles de execução forem passados pro wrapper. Sem custo
    # extra de rede: usa as mesmas funções que o engine já tem.
    smc_quality = None
    if exec_candles:
        try:
            smc_quality = {
                'obs': len(find_order_blocks(exec_candles)),
                'fvgs': len(find_open_fvgs(exec_candles)),
            }
        except Exception:
            smc_quality = None

    score = resultado.get('score')
    votos_favor = resultado.get('votos_favor')
    votos_total = resultado.get('votos_total')

    motivos = []
    if regime_info:
        regime, adx = regime_info
        motivos.append(f"Regime: {regime.upper()}" + (f" (ADX {adx})" if adx is not None else ""))

    estrutura = None
    if resultado.get('choch_direcao') is not None:
        estrutura = 'CHOCH'
    elif resultado.get('bos_direcao') is not None:
        estrutura = 'BOS'
    elif resultado.get('evento_tipo'):
        estrutura = resultado['evento_tipo']
    if estrutura:
        motivos.append(f"Estrutura: {estrutura}")

    if resultado.get('entry_zone_tipo'):
        motivos.append(f"Gatilho: {resultado['entry_zone_tipo']}")

    padrao = indicadores.get('candle_pattern')
    if padrao:
        motivos.append(f"Padrão: {padrao}")

    if score is not None:
        motivos.append(f"Score: {score}/100")
    elif votos_favor is not None and votos_total is not None:
        motivos.append(f"Votos: {votos_favor}/{votos_total} indicadores")

    if resultado.get('mtf_status'):
        motivos.append(f"MTF: {resultado['mtf_status']}")

    # Prob. Acerto real — Monte Carlo pareado com a direção do sinal
    # (prob_alta_pct se COMPRA/alta, prob_baixa_pct se VENDA/baixa).
    # Nada de número decorativo: se o Monte Carlo não rodou (poucos
    # candles), fica None e o front deve mostrar "N/D".
    prob_acerto_pct = None
    if monte_carlo and direcao:
        prob_acerto_pct = (
            monte_carlo.get('prob_alta_pct') if direcao == 'alta'
            else monte_carlo.get('prob_baixa_pct')
        )

    payload = {
        'modo': modo,
        'pair': resultado.get('pair'),
        'direcao': direcao,
        'resumo': {
            'direcao': direcao,
            'par': resultado.get('pair'),
            'timeframe': resultado.get('exec_tf'),
            'prob_acerto_pct': prob_acerto_pct,
        },
        'motivos_principais': motivos,
        'contexto_de_mercado': {
            'regime': regime_info[0].upper() if regime_info else None,
            'adx': regime_info[1] if regime_info else indicadores.get('adx14'),
            'volatilidade': volatilidade,
            'zona_pd': zona_pd,
            'na_killzone': resultado.get('na_killzone'),
            'killzone_nome': resultado.get('killzone_nome'),
            'bias_d1': resultado.get('bias_d1'),
            'bias_h4': resultado.get('bias_h4'),
            'bias_semanal': resultado.get('bias_semanal'),
        },
        'analise_tecnica': {
            'entrada': entry,
            'sl': sl,
            'tp': tp,
            'tp_origem': resultado.get('tp_origem'),
            'rr': rr,
            'rsi14': resultado.get('rsi14') or indicadores.get('rsi14'),
            'atr_multiplo': atr_multiplo,
        },
        'gatilho_energia': {
            'padrao_candle': padrao_candle,
            'vela_rejeicao': vela_rejeicao,
            'energia_confirmada': energia_confirmada,
        },
        'smc_quality': smc_quality,
        'filtros_de_qualidade': _gates_para_filtros(resultado.get('gates')),
        'score_total': {
            'score': score,
            'votos_favor': votos_favor,
            'votos_total': votos_total,
        },
        'breakdown_score': resultado.get('detalhes') or resultado.get('votos_detalhe') or [],
    }

    if monte_carlo:
        payload['monte_carlo'] = monte_carlo
    if ichimoku:
        payload['ichimoku'] = {**ichimoku, 'informativo': True}

    return payload


def salvar_explicacao_ultimo_sinal(db_file, modo, pair, resultado, regime_info=None, exec_candles=None):
    houve_sinal = resultado.get('motivo') == 'entrada' or resultado.get('sinal') is True
    if not houve_sinal:
        return None

    tabela = TABELA_POR_MODO.get(modo)
    if not tabela:
        return None

    try:
        with sqlite3.connect(db_file) as conn:
            row = conn.execute(
                f"SELECT id, created_at FROM {tabela} WHERE pair=? ORDER BY created_at DESC LIMIT 1",
                (pair,),
            ).fetchone()
        if not row:
            return None
        signal_id, created_at = row

        payload = build_explicacao_payload(resultado, modo, regime_info=regime_info, exec_candles=exec_candles)

        with sqlite3.connect(db_file) as conn:
            conn.execute(
                "INSERT OR REPLACE INTO scalp_explicacoes "
                "(signal_id, pair, modo, created_at, payload_json) VALUES (?, ?, ?, ?, ?)",
                (signal_id, pair, modo, created_at, json.dumps(payload, ensure_ascii=False)),
            )
            conn.commit()
        return signal_id
    except Exception as e:
        print(f"[explicacao] erro ao salvar explicacao ({modo}, {pair}): {e}")
        return None




def process_pair_4camadas_com_explicacao(db_file, pair, d1_candles, h4_candles, h1_candles, exec_candles,
                                           exec_tf_label, send_telegram_fn=None):
    resultado = process_pair_4camadas(db_file, pair, d1_candles, h4_candles, h1_candles, exec_candles,
                                       exec_tf_label, send_telegram_fn)
    salvar_explicacao_ultimo_sinal(db_file, '4camadas', pair, resultado, exec_candles=exec_candles)
    return resultado




def process_pair_gates_vortex_com_explicacao(db_file, pair, candles_por_tf, exec_tf_label='M1',
                                               send_telegram_fn=None):
    resultado = process_pair_gates_vortex(db_file, pair, candles_por_tf, exec_tf_label, send_telegram_fn)
    exec_candles_ref = candles_por_tf.get('M1') or candles_por_tf.get('M5')
    salvar_explicacao_ultimo_sinal(db_file, 'gates_vortex', pair, resultado, exec_candles=exec_candles_ref)
    return resultado


explicacao_bp = Blueprint("explicacao_bp", __name__)


def _db_file_explicacao():
    return current_app.config.get('DB_FILE') or current_app.config.get('DB_PATH', '/data/alerts.db')


@explicacao_bp.route("/scalp/sinal/<signal_id>/explicacao", methods=["GET"])
def explicacao_por_id(signal_id):
    try:
        with sqlite3.connect(_db_file_explicacao()) as conn:
            row = conn.execute(
                "SELECT payload_json FROM scalp_explicacoes WHERE signal_id=?", (signal_id,)
            ).fetchone()
    except Exception as e:
        return jsonify({"erro": str(e)}), 500

    if not row:
        return jsonify({"erro": "sinal nao encontrado ou ainda sem explicacao gerada"}), 404
    return jsonify(json.loads(row[0]))


@explicacao_bp.route("/scalp/sinal/ultimo/<modo>/<pair>/explicacao", methods=["GET"])
def explicacao_ultimo(modo, pair):
    if modo not in TABELA_POR_MODO:
        return jsonify({"erro": f"modo desconhecido: {modo}. Use um de {list(TABELA_POR_MODO.keys())}"}), 400
    try:
        with sqlite3.connect(_db_file_explicacao()) as conn:
            row = conn.execute(
                "SELECT payload_json FROM scalp_explicacoes WHERE modo=? AND pair=? "
                "ORDER BY created_at DESC LIMIT 1",
                (modo, pair),
            ).fetchone()
    except Exception as e:
        return jsonify({"erro": str(e)}), 500

    if not row:
        return jsonify({"erro": f"nenhum sinal com explicacao ainda para {pair}/{modo}"}), 404
    return jsonify(json.loads(row[0]))
