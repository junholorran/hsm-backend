# scalp_engine.py
# ─────────────────────────────────────────────────────────────────────────
# Motor de Scalp Ao Vivo — aditivo, não mexe em nada do cascade_engine.
# ─────────────────────────────────────────────────────────────────────────

import sqlite3
import time
import random
import requests
import json
import threading
from flask import Blueprint, jsonify, current_app, request
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
    'gates_vortex': True,            # motor restrito — 8 passos + 7 gates, com trava de contradição
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


def compute_lux_structure_events(candles, swing_size=50):
    """
    Núcleo compartilhado da estrutura LuxAlgo (leg/pivot + BOS/CHoCH).
    Mesma matemática que já existia em compute_lux_structure_bias — só
    passou a retornar a lista de EVENTOS (não só o bias final), pra
    poder distinguir BOS de CHoCH e saber o timestamp de cada quebra.
    Retorna lista de dicts: {'tipo': 'BOS'|'CHoCH', 'direcao': 'alta'|'baixa',
    'nivel': float, 't': timestamp, 'index': int}
    """
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

    swing_high_level = None
    swing_low_level = None
    swing_high_crossed = False
    swing_low_crossed = False
    bias = 'neutro'
    eventos = []

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
            tipo = 'CHoCH' if bias == 'baixa' else 'BOS'
            eventos.append({'tipo': tipo, 'direcao': 'alta', 'nivel': swing_high_level, 't': c['t'], 'index': i})
            bias = 'alta'
            swing_high_crossed = True
        if swing_low_level is not None and not swing_low_crossed and c['c'] < swing_low_level:
            tipo = 'CHoCH' if bias == 'alta' else 'BOS'
            eventos.append({'tipo': tipo, 'direcao': 'baixa', 'nivel': swing_low_level, 't': c['t'], 'index': i})
            bias = 'baixa'
            swing_low_crossed = True

    return eventos


def compute_lux_structure_bias(candles, swing_size=50):
    """Wrapper fino sobre compute_lux_structure_events — mesmo retorno
    de sempre ('alta'/'baixa'/'neutro'), preservado pra não quebrar
    nenhum chamador existente. Zero mudança de comportamento."""
    eventos = compute_lux_structure_events(candles, swing_size=swing_size)
    if not eventos:
        return 'neutro'
    return eventos[-1]['direcao']


def compute_lux_internal_structure(candles, swing_size=5):
    """
    Item 1 do ticket — 'estrutura interna' (janela curta, default 5
    barras), reaproveitando compute_lux_structure_events sem duplicar
    a lógica. Retorna a lista de eventos (BOS/CHoCH) dessa janela curta.
    Isso é LUX_INTERNAL_CHoCH — mecanismo separado e paralelo de
    detect_choch_after_sweep() (SWEEP_BASED_CHoCH), que continua
    intocado.
    """
    return compute_lux_structure_events(candles, swing_size=swing_size)


def compute_lux_premium_discount(candles, swing_size=50):
    """
    Item 2 do ticket — versão isolada e comparável do Premium/Discount,
    aproximando o mecanismo real do LuxAlgo (trailing.top/trailing.bottom
    = extremos desde o ÚLTIMO PIVOT OPOSTO, não janela fixa de N candles).
    NÃO substitui compute_premium_discount()/compute_zona_movel() — são
    mantidas intactas pra comparação lado a lado (zona_atual_kairos vs
    zona_luxalgo).

    Mecanismo: reaproveita compute_lux_structure_events() pra achar os
    pivots (mesmos pivots que geram BOS/CHoCH), e entre dois eventos de
    direção oposta consecutivos, calcula o máximo/mínimo real percorrido
    pelo candles nesse intervalo (equivalente ao trailing.top/bottom que
    o Pine atualiza barra a barra).

    Retorna: {'top', 'bottom', 'premium_bottom', 'discount_top',
    'equilibrium_top', 'equilibrium_bottom'} ou None se não houver
    dados suficientes.
    """
    eventos = compute_lux_structure_events(candles, swing_size=swing_size)
    if not eventos:
        return None

    ultimo_evento = eventos[-1]
    idx_inicio = ultimo_evento['index']

    janela = candles[idx_inicio:]
    if not janela:
        return None

    top = max(c['h'] for c in janela)
    bottom = min(c['l'] for c in janela)

    if top == bottom:
        return None

    return {
        'top': top, 'bottom': bottom,
        'premium_bottom': 0.95 * top + 0.05 * bottom,
        'discount_top': 0.95 * bottom + 0.05 * top,
        'equilibrium_top': 0.525 * top + 0.475 * bottom,
        'equilibrium_bottom': 0.525 * bottom + 0.475 * top,
        'pivot_evento': ultimo_evento,
    }


def classificar_zona_lux(preco, zona):
    """Classifica um preço como 'premium', 'discount' ou 'equilibrium',
    usando a zona calculada por compute_lux_premium_discount(). Puramente
    de leitura, sem efeito colateral."""
    if not zona:
        return None
    if preco >= zona['premium_bottom']:
        return 'premium'
    if preco <= zona['discount_top']:
        return 'discount'
    return 'equilibrium'


def find_open_fvgs_adaptive(exec_candles, lookback=100, extend_bars=1):
    """
    Item 3 do ticket — versão isolada do FVG com threshold ADAPTATIVO,
    replicando drawFairValueGaps() do LuxAlgo: em vez de um gap_pct
    mínimo fixo (find_open_fvgs() usa MIN_FVG_GAP_PCT=0.0005 fixo),
    o threshold é a média cumulativa histórica do deslocamento
    percentual absoluto do candle do meio, multiplicada por 2 —
    recalculada a cada candle, conforme o histórico acumulado até ali.
    NÃO substitui find_open_fvgs() — mantida intacta pra comparação
    (FVG_MODE=legacy vs FVG_MODE=luxalgo).
    """
    candles = exec_candles[-lookback:] if len(exec_candles) > lookback else exec_candles
    n = len(candles)
    if n < 3:
        return []

    abertas = []
    soma_abs_delta_pct = 0.0

    for i in range(1, n - 1):
        prev, meio, nxt = candles[i - 1], candles[i], candles[i + 1]

        delta_pct = ((meio['c'] - meio['o']) / (meio['o'] * 100)) if meio['o'] else 0.0
        soma_abs_delta_pct += abs(delta_pct)
        threshold = (soma_abs_delta_pct / (i + 1)) * 2 if (i + 1) else 0.0

        if nxt['l'] > prev['h'] and meio['c'] > prev['h'] and delta_pct > threshold:
            top, bottom = nxt['l'], prev['h']
            preenchida = any(c['l'] <= bottom for c in candles[i + 2:i + 2 + extend_bars + lookback])
            if not preenchida:
                abertas.append({
                    'tipo': 'FVG_bullish', 'top': round(top, 6), 'bottom': round(bottom, 6),
                    't': meio['t'], 'threshold_usado': round(threshold, 8), 'delta_pct': round(delta_pct, 8),
                })
        if nxt['h'] < prev['l'] and meio['c'] < prev['l'] and -delta_pct > threshold:
            top, bottom = prev['l'], nxt['h']
            preenchida = any(c['h'] >= top for c in candles[i + 2:i + 2 + extend_bars + lookback])
            if not preenchida:
                abertas.append({
                    'tipo': 'FVG_bearish', 'top': round(top, 6), 'bottom': round(bottom, 6),
                    't': meio['t'], 'threshold_usado': round(threshold, 8), 'delta_pct': round(delta_pct, 8),
                })

    return abertas


def find_equal_highs_lows_luxalgo(candles, length=3, atr_mult=0.1, atr_period=200):
    """
    Item 4 do ticket — mesma lógica de find_equal_highs_lows(), só troca
    o período do ATR de 14 (Kairos legado) pra 200 (LuxAlgo real).
    Reaproveita detect_exec_swings() e compute_atr() sem duplicar nada.
    NÃO substitui find_equal_highs_lows() — mantida intacta.
    """
    atr_series = compute_atr(candles, atr_period)
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


def avaliar_legacy_decision_layer(candles_swing, candles_exec):
    """
    Espelho de avaliar_vortex_decision_layer(), mas usando SÓ as funções
    LEGADAS do Kairos (as que já existiam antes deste ticket), pra dar
    uma comparação maçã-com-maçã contra o caminho LUXALGO/VORTEX:
      - bias: compute_lux_structure_bias() — ATENÇÃO: essa função JÁ
        EXISTIA antes deste ticket e já era matematicamente idêntica ao
        leg() do LuxAlgo (confirmado na auditoria). Por isso o campo
        'bias' aqui SEMPRE vai bater com o bias do caminho LUXALGO —
        isso não é bug, é o resultado esperado de uma peça que já
        estava correta antes de qualquer mudança.
      - zona: compute_premium_discount()/compute_zona_movel() — janela
        fixa de 20 candles, split 50/50 (diferente do trailing dinâmico
        do LuxAlgo).
      - choch: precisa de um 'sweep' — usa detect_sweep_in_zone() sobre
        a mesma zona legada, depois detect_choch_after_sweep(). Esse é
        o mecanismo SWEEP_BASED_CHoCH, oficialmente distinto do
        LUX_INTERNAL_CHoCH.
      - fvg: find_open_fvgs() — threshold fixo.
    Também NUNCA gera entrada — mesma restrição do caminho Vortex,
    porque SL/TP legado (calcular_sl_estrito) pertence a um pipeline
    diferente (SFP causal) que não faz parte deste comparativo
    estrutural. Não decide nada de produção, não chama
    process_pair_gates_vortex() nem process_pair_4camadas().
    """
    resultado = {
        'decisao': 'SEM_ENTRADA', 'motivo_rejeicao': None,
        'bias': None, 'zona': None, 'choch': None, 'fvg_candidatos': [],
        'entry': None, 'sl': None, 'tp': None,
    }

    bias = compute_lux_structure_bias(candles_swing, swing_size=50)
    resultado['bias'] = bias
    if bias == 'neutro':
        resultado['motivo_rejeicao'] = 'BIAS_FAIL'
        return resultado

    zona_calc = compute_premium_discount(candles_swing)
    preco_atual = candles_swing[-1]['c'] if candles_swing else None
    zona = None
    if zona_calc and preco_atual is not None:
        if preco_atual >= zona_calc['equilibrium']:
            zona = 'premium'
        else:
            zona = 'discount'
    resultado['zona'] = zona

    if bias == 'alta' and zona != 'discount':
        resultado['motivo_rejeicao'] = 'ZONE_FAIL'
        return resultado
    if bias == 'baixa' and zona != 'premium':
        resultado['motivo_rejeicao'] = 'ZONE_FAIL'
        return resultado

    sweep = detect_sweep_in_zone(candles_exec, zona_calc) if zona_calc else None
    choch = detect_choch_after_sweep(candles_exec, sweep) if sweep else None
    resultado['choch'] = choch
    if not choch or choch['direcao'] != bias:
        resultado['motivo_rejeicao'] = 'NO_CHOCH'
        return resultado

    fvgs = find_open_fvgs(candles_exec)
    tipo_fvg_desejado = 'FVG_bullish' if bias == 'alta' else 'FVG_bearish'
    candidatos = [f for f in fvgs if f['tipo'] == tipo_fvg_desejado]
    resultado['fvg_candidatos'] = candidatos

    if not candidatos:
        resultado['motivo_rejeicao'] = 'NO_VALID_FVG'
        return resultado

    resultado['motivo_rejeicao'] = 'FVG_CHOCH_RELATION_UNKNOWN'
    return resultado


def avaliar_vortex_decision_layer(candles_swing, candles_internal, direcao_desejada=None):
    """
    Item 9 do ticket — VORTEX DECISION LAYER (camada de composição).
    Junta bias + zona + CHoCH interno + candidatos de FVG, mas NUNCA
    gera uma entrada completa, porque:
      - VORTEX_FVG_CHOCH_RELATION = UNKNOWN (sem evidência de como a
        Vortex associa um FVG específico a um CHoCH específico)
      - SL_VORTEX = UNKNOWN (LuxAlgo não calcula SL, e o único exemplo
        real não é suficiente pra provar uma fórmula)
      - RR_VORTEX = UNKNOWN (1 amostra só, R:R=2.0 não é regra confirmada)
    Por isso essa função para no ponto do FVG e retorna os candidatos
    sem escolher nenhum — é um scaffold de auditoria/telemetria, não
    um gerador de sinal. Não decide nada de produção, não substitui
    process_pair_4camadas() nem process_pair_gates_vortex().
    """
    resultado = {
        'decisao': 'SEM_ENTRADA', 'motivo_rejeicao': None,
        'bias': None, 'zona': None, 'internal_choch': None,
        'fvg_candidatos': [], 'entry': None, 'sl': None, 'tp': None,
    }

    bias = compute_lux_structure_bias(candles_swing, swing_size=50)
    resultado['bias'] = bias
    if bias == 'neutro':
        resultado['motivo_rejeicao'] = 'BIAS_FAIL'
        return resultado

    zona_calc = compute_lux_premium_discount(candles_swing, swing_size=50)
    preco_atual = candles_swing[-1]['c'] if candles_swing else None
    zona = classificar_zona_lux(preco_atual, zona_calc) if zona_calc and preco_atual is not None else None
    resultado['zona'] = zona

    if bias == 'alta' and zona != 'discount':
        resultado['motivo_rejeicao'] = 'ZONE_FAIL'
        return resultado
    if bias == 'baixa' and zona != 'premium':
        resultado['motivo_rejeicao'] = 'ZONE_FAIL'
        return resultado

    eventos_internos = compute_lux_internal_structure(candles_internal, swing_size=5)
    choch_relevante = None
    for ev in reversed(eventos_internos):
        if ev['tipo'] == 'CHoCH' and ev['direcao'] == bias:
            choch_relevante = ev
            break
    resultado['internal_choch'] = choch_relevante
    if not choch_relevante:
        resultado['motivo_rejeicao'] = 'NO_CHOCH'
        return resultado

    fvgs = find_open_fvgs_adaptive(candles_internal)
    tipo_fvg_desejado = 'FVG_bullish' if bias == 'alta' else 'FVG_bearish'
    candidatos = [f for f in fvgs if f['tipo'] == tipo_fvg_desejado]
    resultado['fvg_candidatos'] = candidatos

    if not candidatos:
        resultado['motivo_rejeicao'] = 'NO_VALID_FVG'
        return resultado

    # ── BLOQUEIO INTENCIONAL — item 8 do ticket. Existem candidatos de
    # FVG, mas não há regra determinística comprovada pra escolher qual
    # deles pertence ao "mesmo movimento" do CHoCH interno. Não inventar. ──
    resultado['motivo_rejeicao'] = 'FVG_CHOCH_RELATION_UNKNOWN'
    return resultado


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


HTF_PREMIUM_DISCOUNT_LOOKBACK = 90
HTF_EQUILIBRIUM_BUFFER_PCT = 0.03  # 3% de banda neutra em torno do equilíbrio


def compute_htf_narrative(d1, h4, h1):
    """
    Camada de NARRATIVA (contexto), não de gatilho.

    Interpreta D1 -> H4 -> H1 (hierarquia estrita, D1 manda) e devolve
    bias/strength/premium-discount/liquidez, sem decidir entrada.

    Determinística, sem API, sem banco, sem Telegram, sem efeitos
    colaterais — só lê as listas de candles recebidas. Reaproveita
    compute_bias_from_swings() e compute_zona_movel() já existentes,
    não duplica detector nenhum.

    Se D1 não tiver candles suficientes, devolve bias NEUTRAL com
    reasons explicando o motivo (nunca inventa contexto).
    """
    reasons = []

    if not d1 or len(d1) < (SWING_LOOKBACK * 2 + 5):
        return {
            'bias': 'NEUTRAL', 'strength': 'WEAK',
            'd1_bias': 'neutro', 'h4_bias': 'neutro', 'h1_bias': 'neutro',
            'premium_discount': {'state': 'EQUILIBRIUM', 'value': None},
            'liquidity': {'buy_side': None, 'sell_side': None, 'nearest_target': None, 'nearest_target_side': None},
            'alignment': {'aligned': False, 'score': 0},
            'long_allowed': True, 'short_allowed': True,
            'reasons': ['D1 sem candles suficientes — narrativa não pôde ser calculada, contexto neutro por padrão'],
        }

    d1_bias = compute_bias_from_swings(d1)
    h4_bias = compute_bias_from_swings(h4) if h4 else 'neutro'
    h1_bias = compute_bias_from_swings(h1) if h1 else 'neutro'

    # ── Hierarquia D1 > H4 > H1 — D1 nunca é sobrescrito, só enfraquecido ──
    if d1_bias == 'neutro':
        bias_final = 'NEUTRAL'
        strength = 'WEAK'
        reasons.append('D1 sem bias estrutural definido — contexto neutro')
    elif h4_bias != 'neutro' and h4_bias != d1_bias:
        # H4 contradiz D1 — conflito relevante
        if h1_bias == h4_bias:
            bias_final = 'NEUTRAL'
            strength = 'WEAK'
            reasons.append(f'D1={d1_bias} mas H4 e H1 concordam em {h4_bias} — conflito forte, contexto neutralizado')
        else:
            bias_final = 'LONG' if d1_bias == 'alta' else 'SHORT'
            strength = 'WEAK'
            reasons.append(f'D1={d1_bias} contrariado por H4={h4_bias} — bias mantido por hierarquia, mas fraco, não liberar entrada forte')
    else:
        bias_final = 'LONG' if d1_bias == 'alta' else 'SHORT'
        if h1_bias == d1_bias:
            strength = 'STRONG'
            reasons.append(f'D1={d1_bias}, H4 confirma, H1 confirma — alinhamento total')
        else:
            strength = 'MODERATE'
            reasons.append(f'D1={d1_bias}, H4 confirma, H1={h1_bias} diverge — confirmação parcial')

    # ── Premium / Discount — range estrutural do D1 (Donchian), não candle isolado ──
    donch = compute_zona_movel(d1, lookback=min(HTF_PREMIUM_DISCOUNT_LOOKBACK, len(d1)))
    premium_discount = {'state': 'EQUILIBRIUM', 'value': None}
    liquidity = {'buy_side': None, 'sell_side': None, 'nearest_target': None, 'nearest_target_side': None}

    if donch:
        preco_atual = d1[-1]['c']
        top, bottom, eq = donch['top'], donch['bottom'], (donch['top'] + donch['bottom']) / 2
        largura = (top - bottom) or 1
        posicao_pct = (preco_atual - bottom) / largura  # 0 = bottom, 1 = top
        premium_discount['value'] = round(posicao_pct, 4)

        banda = HTF_EQUILIBRIUM_BUFFER_PCT
        if posicao_pct >= 0.5 + banda:
            premium_discount['state'] = 'PREMIUM'
        elif posicao_pct <= 0.5 - banda:
            premium_discount['state'] = 'DISCOUNT'
        else:
            premium_discount['state'] = 'EQUILIBRIUM'

        liquidity['buy_side'] = top
        liquidity['sell_side'] = bottom
        dist_top = abs(top - preco_atual)
        dist_bottom = abs(preco_atual - bottom)
        if dist_top <= dist_bottom:
            liquidity['nearest_target'] = top
            liquidity['nearest_target_side'] = 'buy_side'
        else:
            liquidity['nearest_target'] = bottom
            liquidity['nearest_target_side'] = 'sell_side'

        if bias_final == 'LONG' and premium_discount['state'] == 'PREMIUM':
            reasons.append('Contexto LONG mas preço em Premium — preferir aguardar Discount/Equilíbrio antes de perseguir')
        elif bias_final == 'SHORT' and premium_discount['state'] == 'DISCOUNT':
            reasons.append('Contexto SHORT mas preço em Discount — preferir aguardar Premium/Equilíbrio antes de perseguir')

    alignment_score = sum([
        1 if d1_bias != 'neutro' else 0,
        1 if h4_bias == d1_bias and d1_bias != 'neutro' else 0,
        1 if h1_bias == d1_bias and d1_bias != 'neutro' else 0,
    ])
    alignment = {'aligned': alignment_score == 3, 'score': alignment_score}

    # ── Autorização de direção — só bloqueia o lado CONTRA um contexto forte/moderado ──
    long_allowed = True
    short_allowed = True
    if bias_final == 'LONG' and strength in ('STRONG', 'MODERATE'):
        short_allowed = False
    elif bias_final == 'SHORT' and strength in ('STRONG', 'MODERATE'):
        long_allowed = False

    return {
        'bias': bias_final,
        'strength': strength,
        'd1_bias': d1_bias, 'h4_bias': h4_bias, 'h1_bias': h1_bias,
        'premium_discount': premium_discount,
        'liquidity': liquidity,
        'alignment': alignment,
        'long_allowed': long_allowed,
        'short_allowed': short_allowed,
        'reasons': reasons,
    }


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
    """Mapeia alvos que o preço realmente pode buscar primeiro.

    Regras:
    - EQH/EQL: o próprio nível é o alvo.
    - Zona D1: usa a BORDA DE ENTRADA da zona (primeiro obstáculo), não a
      borda mais distante.
    - OB: só usa OB contrário ao trade e a borda mais próxima do preço.
    Retorna tuplas (origem, nível)."""
    candidatos = []

    try:
        for eq in find_equal_highs_lows(exec_candles):
            nivel = float(eq['nivel'])
            if direcao == 'alta' and nivel > entry:
                candidatos.append((eq['tipo'], nivel))
            elif direcao == 'baixa' and nivel < entry:
                candidatos.append((eq['tipo'], nivel))
    except Exception:
        pass

    try:
        for banda in compute_d1_zones(d1_candles or []):
            if direcao == 'alta' and banda['bottom'] > entry:
                candidatos.append(('zona_d1', banda['bottom']))
            elif direcao == 'baixa' and banda['top'] < entry:
                candidatos.append(('zona_d1', banda['top']))
    except Exception:
        pass

    try:
        for ob in find_order_blocks(exec_candles):
            tipo = ob.get('tipo', '')
            if direcao == 'alta' and tipo == 'OB_bearish' and ob['bottom'] > entry:
                # Primeiro toque numa oferta acima do preço.
                candidatos.append(('OB_oferta', ob['bottom']))
            elif direcao == 'baixa' and tipo == 'OB_bullish' and ob['top'] < entry:
                # Primeiro toque numa demanda abaixo do preço.
                candidatos.append(('OB_demanda', ob['top']))
    except Exception:
        pass

    # Remove níveis duplicados/quase iguais para não contar o mesmo alvo
    # várias vezes por fontes diferentes.
    unicos = []
    tolerancia = max(abs(entry) * 0.00005, 1e-12)
    for origem, nivel in sorted(candidatos, key=lambda x: abs(x[1] - entry)):
        if not any(abs(nivel - n) <= tolerancia for _, n in unicos):
            unicos.append((origem, nivel))
    return unicos


def calcular_tp_dinamico(direcao, entry, sl, exec_candles, d1_candles, min_rr=None):
    """Escolhe TP por liquidez real, depois Monte Carlo, depois RR mínimo.

    Nunca devolve um alvo fora da direção do trade e nunca ultrapassa
    TP_DINAMICO_MAX_RR. A origem é devolvida para auditoria/replay."""
    min_rr = min_rr if min_rr is not None else MIN_RR_GATE
    risco = abs(entry - sl)
    if risco <= 0:
        return None, 'sem_risco_valido'

    validos = []
    for origem, nivel in _find_liquidez_alvo(direcao, entry, exec_candles, d1_candles):
        distancia = abs(nivel - entry)
        rr = distancia / risco
        na_direcao = nivel > entry if direcao == 'alta' else nivel < entry
        if na_direcao and min_rr <= rr <= TP_DINAMICO_MAX_RR:
            validos.append((origem, nivel, rr))

    if validos:
        validos.sort(key=lambda x: x[2])
        origem, nivel, rr = validos[0]
        return round(nivel, 6), f'liquidez_real:{origem} (RR {rr:.2f})'

    try:
        mc = compute_monte_carlo(exec_candles)
    except Exception:
        mc = None

    if mc:
        alvo_mc = mc['cenario_otimista'] if direcao == 'alta' else mc['cenario_pessimista']
        na_direcao = alvo_mc > entry if direcao == 'alta' else alvo_mc < entry
        distancia = abs(alvo_mc - entry)
        rr = distancia / risco if risco > 0 else 0
        if na_direcao and min_rr <= rr <= TP_DINAMICO_MAX_RR:
            return round(alvo_mc, 6), f'monte_carlo (RR {rr:.2f})'

    tp_fallback = entry + risco * min_rr if direcao == 'alta' else entry - risco * min_rr
    return round(tp_fallback, 6), f'fallback_rr_minimo ({min_rr})'


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
        # ── FIX (13/08): gerenciar_trades_abertos() é genérica e roda pra
        # TODAS as tabelas de MODOS_SCALP, esperando as colunas tp,
        # be_movido, parcial_feita em cada uma. Esta tabela só tinha
        # tp1/tp2 (sem "tp") e não tinha be_movido/parcial_feita —
        # causava "no such column" toda vez que o gerenciador rodava
        # pra este modo. Adiciona as colunas que faltam; "tp" é
        # preenchida no INSERT (ver _save_sfp_liquidez_signal) com o
        # valor de tp1, que é o alvo primário usado pra resolver
        # win/loss. ──
        for alter_sql in [
            "ALTER TABLE scalp_sfp_liquidez_signal_state ADD COLUMN tp REAL",
            "ALTER TABLE scalp_sfp_liquidez_signal_state ADD COLUMN be_movido INTEGER DEFAULT 0",
            "ALTER TABLE scalp_sfp_liquidez_signal_state ADD COLUMN parcial_feita INTEGER DEFAULT 0",
            "ALTER TABLE scalp_sfp_liquidez_signal_state ADD COLUMN status_gestao TEXT DEFAULT ''",
        ]:
            try:
                conn.execute(alter_sql)
                conn.commit()
            except Exception:
                pass


def _save_sfp_liquidez_signal(db_file, pair, exec_tf_label, resultado, alerted):
    try:
        signal_id = f"sfp_{pair}_{int(time.time()*1000)}"
        with sqlite3.connect(db_file) as conn:
            conn.execute('''
                INSERT INTO scalp_sfp_liquidez_signal_state
                    (id, pair, created_at, exec_tf, direcao, entry, sl, tp, tp1, tp2, bias_context, alerted)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ''', (
                signal_id, pair, int(time.time()), exec_tf_label,
                resultado['direcao'], resultado['entry'], resultado['sl'],
                resultado.get('tp1'), resultado['tp1'], resultado['tp2'], resultado.get('bias_context'),
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

    # cripto: últimos 3-7 dias COMPLETOS em D1, excluindo o dia atual.
    # Importante: o timestamp do D1 é a ABERTURA do dia. O cutoff tem de
    # ser o fim do último dia usado na liquidez; usar janela[-1]['t']
    # deixava parte desse próprio dia disponível para o SFP e contaminava
    # a referência (self-reference).
    d1 = candles_por_tf.get('D1')
    if not d1 or len(d1) < 4:
        return None
    janela = d1[-8:-1] if len(d1) >= 8 else d1[:-1]
    if len(janela) < 3:
        return None
    ultimo_dia_liquidez_ts = janela[-1]['t']
    cutoff_ts = ultimo_dia_liquidez_ts + 24 * 60 * 60 * 1000
    return {
        'high': max(c['h'] for c in janela),
        'low': min(c['l'] for c in janela),
        'sessao': f'ultimos_{len(janela)}_dias', 'dia': None,
        'cutoff_ts': cutoff_ts,
        'liquidez_inicio_ts': janela[0]['t'],
        'liquidez_fim_ts': ultimo_dia_liquidez_ts + 24 * 60 * 60 * 1000 - 1,
        'candles_liquidez': janela,
    }


def _garantir_tabela_audit_breakout_cancel(db_file):
    """Cria a tabela de auditoria se não existir. Auto-blindada."""
    try:
        with sqlite3.connect(db_file) as conn:
            conn.execute('''
                CREATE TABLE IF NOT EXISTS audit_breakout_cancel (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    pair TEXT NOT NULL,
                    cycle_ts INTEGER NOT NULL,
                    candle_event_ts INTEGER,
                    bias TEXT,
                    midnight_open REAL,
                    high_liq REAL,
                    low_liq REAL,
                    motivo TEXT
                )
            ''')
            conn.commit()
    except Exception as e:
        print(f"[scalp_engine audit_breakout] erro ao criar tabela: {e}")


def _registrar_audit_breakout_cancel(db_file, pair, cycle_ts, candle_event_ts, bias,
                                      midnight_open, high_liq, low_liq, motivo):
    """
    Auditoria PURA — 1 linha por ciclo em que breakout_cancela_analise for
    o motivo. SEM dedup (proposital nesta fase). SEM UPSERT (INSERT
    simples — cada chamada é uma linha nova, mesmo repetindo candle_event_ts).
    Não decide nada, não é lida por nenhuma outra função do pipeline.
    Fail-open: erro aqui só é logado, nunca propaga.
    """
    _garantir_tabela_audit_breakout_cancel(db_file)
    try:
        with sqlite3.connect(db_file) as conn:
            conn.execute('''
                INSERT INTO audit_breakout_cancel
                    (pair, cycle_ts, candle_event_ts, bias, midnight_open, high_liq, low_liq, motivo)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            ''', (pair, cycle_ts, candle_event_ts, bias, midnight_open, high_liq, low_liq, motivo))
            conn.commit()
    except Exception as e:
        print(f"[scalp_engine audit_breakout] erro ao registrar {pair}: {e}")


def validar_sfp_estrito(candles_sfp, liquidez, direcao_permitida):
    """Valida SFP sem auto-referência e sem aceitar SFP invalidado depois.

    A janela é percorrida em ordem cronológica. Cada sweep/reclaim válido
    atualiza o candidato. Se, depois dele, houver fechamento de corpo além
    da liquidez, o nível é considerado rompido e toda a análise é cancelada.
    Assim o engine pode usar o SFP mais recente que ainda esteja válido.
    """
    if not liquidez:
        return None, 'sem_liquidez_mapeada', None

    cutoff_ts = liquidez.get('cutoff_ts')
    candles_pos = [c for c in candles_sfp if cutoff_ts is None or c['t'] >= cutoff_ts]
    high_liq, low_liq = liquidez['high'], liquidez['low']
    ultimo_sfp = None

    for c in candles_pos:
        if direcao_permitida == 'baixa':
            if c['c'] > high_liq:
                return None, 'breakout_cancela_analise', c['t']
            if c['h'] > high_liq and c['c'] < high_liq:
                ultimo_sfp = {
                    'tipo': 'SFP_venda', 'nivel': high_liq,
                    'sl_pavio': c['h'], 't': c['t'],
                }
        elif direcao_permitida == 'alta':
            if c['c'] < low_liq:
                return None, 'breakout_cancela_analise', c['t']
            if c['l'] < low_liq and c['c'] > low_liq:
                ultimo_sfp = {
                    'tipo': 'SFP_compra', 'nivel': low_liq,
                    'sl_pavio': c['l'], 't': c['t'],
                }

    if ultimo_sfp:
        return ultimo_sfp, 'sfp_confirmado', None
    return None, 'sem_sfp_ainda', None


def _diagnostico_detalhado_sfp(candles_sfp, liquidez, direcao_permitida, tf_label):
    """
    Observador puro — roda em paralelo à validar_sfp_estrito, sem alterar
    NENHUM comportamento de decisão. Responde objetivamente aos Casos A-E:
    A) nunca tocou a liquidez, B) tocou mas não fechou de volta (sweep sem
    reclaim), C) fechou além (breakout), D) varreu e voltou (SFP), E) nem
    isso — candles insuficientes.
    """
    diag = {
        'timeframe': tf_label, 'high_liq': None, 'low_liq': None, 'cutoff_ts': None,
        'candles_analisados': 0, 'maior_high': None, 'menor_low': None,
        'tocou_high_liq': False, 'tocou_low_liq': False,
        'fechou_fora_high': False, 'fechou_fora_low': False,
        'fechou_de_volta_high': False, 'fechou_de_volta_low': False,
        'caso': None, 'candle_responsavel': None,
    }
    if not liquidez or not candles_sfp:
        diag['caso'] = 'E_sem_dados'
        return diag

    cutoff_ts = liquidez.get('cutoff_ts')
    high_liq, low_liq = liquidez['high'], liquidez['low']
    candles_pos = [c for c in candles_sfp if cutoff_ts is None or c['t'] > cutoff_ts]

    diag.update({'high_liq': high_liq, 'low_liq': low_liq, 'cutoff_ts': cutoff_ts,
                 'candles_analisados': len(candles_pos)})
    if not candles_pos:
        diag['caso'] = 'E_sem_candles_apos_cutoff'
        return diag

    diag['maior_high'] = max(c['h'] for c in candles_pos)
    diag['menor_low'] = min(c['l'] for c in candles_pos)

    lado_relevante = 'high' if direcao_permitida == 'baixa' else 'low'
    nivel = high_liq if lado_relevante == 'high' else low_liq

    for c in candles_pos:
        if lado_relevante == 'high':
            if c['h'] > nivel:
                diag['tocou_high_liq'] = True
                if c['c'] > nivel:
                    diag['fechou_fora_high'] = True
                    diag['caso'] = 'C_breakout_confirmado'
                    diag['candle_responsavel'] = {'t': c['t'], 'h': c['h'], 'l': c['l'], 'c': c['c']}
                    return diag
                else:
                    diag['fechou_de_volta_high'] = True
                    diag['caso'] = 'D_sfp_confirmado'
                    diag['candle_responsavel'] = {'t': c['t'], 'h': c['h'], 'l': c['l'], 'c': c['c']}
                    return diag
        else:
            if c['l'] < nivel:
                diag['tocou_low_liq'] = True
                if c['c'] < nivel:
                    diag['fechou_fora_low'] = True
                    diag['caso'] = 'C_breakout_confirmado'
                    diag['candle_responsavel'] = {'t': c['t'], 'h': c['h'], 'l': c['l'], 'c': c['c']}
                    return diag
                else:
                    diag['fechou_de_volta_low'] = True
                    diag['caso'] = 'D_sfp_confirmado'
                    diag['candle_responsavel'] = {'t': c['t'], 'h': c['h'], 'l': c['l'], 'c': c['c']}
                    return diag

    diag['caso'] = 'A_nunca_tocou' if not diag['tocou_high_liq'] and not diag['tocou_low_liq'] else 'B_tocou_sem_reclaim'
    return diag


def init_sfp_diagnostico_db(db_file):
    try:
        with sqlite3.connect(db_file) as conn:
            conn.execute('''
                CREATE TABLE IF NOT EXISTS scalp_gates_vortex_sfp_diagnostico (
                    pair TEXT PRIMARY KEY,
                    direcao_permitida TEXT,
                    bias_context TEXT,
                    payload_json TEXT,
                    updated_at INTEGER
                )
            ''')
            conn.commit()
    except Exception as e:
        print(f"[scalp_engine sfp_diag] erro ao criar tabela: {e}")


def _registrar_diagnostico_sfp(db_file, pair, direcao_permitida, bias_context, diag):
    init_sfp_diagnostico_db(db_file)
    try:
        with sqlite3.connect(db_file) as conn:
            conn.execute('''
                INSERT INTO scalp_gates_vortex_sfp_diagnostico (pair, direcao_permitida, bias_context, payload_json, updated_at)
                VALUES (?, ?, ?, ?, ?)
                ON CONFLICT(pair) DO UPDATE SET
                    direcao_permitida=excluded.direcao_permitida,
                    bias_context=excluded.bias_context,
                    payload_json=excluded.payload_json,
                    updated_at=excluded.updated_at
            ''', (pair, direcao_permitida, bias_context, json.dumps(diag, ensure_ascii=False), int(time.time())))
            conn.commit()
    except Exception as e:
        print(f"[scalp_engine sfp_diag] erro ao registrar {pair}: {e}")


def sfp_diagnostico_report(db_file, pair=None):
    init_sfp_diagnostico_db(db_file)
    try:
        with sqlite3.connect(db_file) as conn:
            cursor = conn.cursor()
            if pair:
                cursor.execute('SELECT pair, direcao_permitida, bias_context, payload_json, updated_at FROM scalp_gates_vortex_sfp_diagnostico WHERE pair=?', (pair,))
            else:
                cursor.execute('SELECT pair, direcao_permitida, bias_context, payload_json, updated_at FROM scalp_gates_vortex_sfp_diagnostico')
            rows = cursor.fetchall()
    except Exception as e:
        return {'erro': str(e)}

    resultado = {}
    for p, direcao, bias_context, payload_json, updated_at in rows:
        resultado[p] = {
            'direcao_permitida': direcao, 'bias_context': bias_context,
            'diagnostico': json.loads(payload_json), 'updated_at': updated_at,
        }
    return resultado



def validar_sfp_cascata_tf(candles_por_tf, liquidez, direcao_permitida):
    """
    Tenta achar o SFP em cascata: M15 primeiro (sweep mais confiável,
    menos ruído), se não achar cai pro M5, se não achar cai pro M1 —
    maximiza oportunidade sem abrir mão de tentar o TF mais limpo
    primeiro. Se qualquer TF confirmar breakout real, cancela na hora
    (não faz sentido procurar SFP num nível que já foi rompido de vez).
    Retorna (sfp, motivo, timeframe_usado, candle_ts_evento) — o 4º valor
    é o timestamp do candle que causou 'breakout_cancela_analise' (só
    usado nesse caminho; None nos demais), capturado direto na origem em
    validar_sfp_estrito(), sem redescoberta posterior. Instrumentação
    pura — não influencia nenhuma decisão do pipeline.
    """
    ordem_tfs = ['M15', 'M5', 'M1']
    ultimo_motivo = 'sem_candles_sfp'

    for tf_label in ordem_tfs:
        candles_tf = candles_por_tf.get(tf_label)
        if not candles_tf:
            continue

        sfp, motivo, candle_ts_evento = validar_sfp_estrito(candles_tf, liquidez, direcao_permitida)

        if sfp:
            return sfp, motivo, tf_label, candle_ts_evento

        if motivo == 'breakout_cancela_analise':
            return None, motivo, tf_label, candle_ts_evento

        ultimo_motivo = motivo

    return None, ultimo_motivo, None, None


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
# PIPELINE DE GATES (A-G) — "modelo avançado da Vortex", com uma diferença
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
GATE_C_MONTE_CARLO_MIN_PROB = 65  # filtro direcional real; validar/ajustar pelo replay, não tratar como probabilidade calibrada
GATE_E_MIN_RR = 2.0
GATE_D_MIN_OBS = 1
GATE_D_MIN_FVGS = 1


def esta_em_hora_toxica_estrita(candles_referencia, pair=None):
    """Horas tóxicas exatas do pipeline de Gates (07:00 e 23:00 UTC) —
    separado da lógica de killzone (bônus) já usada nos outros modos,
    porque aqui o requisito é BLOQUEAR, não só somar/subtrair pontos.

    Só se aplica a XAU/metal — o conceito de "troca de sessão com baixa
    liquidez" vem do fechamento/abertura de sessões tradicionais
    (Londres/NY), que não existe em cripto (mercado 24/7, sem fechamento
    de sessão real). Pra cripto, esse bloqueio nunca se aplica.
    """
    if pair is not None and get_asset_class(pair) == 'crypto':
        return False
    if not candles_referencia:
        return False
    dt = datetime.fromtimestamp(candles_referencia[-1]['t'] / 1000, tz=timezone.utc)
    return dt.hour in HORAS_TOXICAS_UTC


GATES_STALENESS_MAX_SEG = 90  # antes 30 — realista pro ciclo em lote (7-9 pares, várias chamadas cada)


def dados_obsoletos(candles, max_latencia_seg=GATES_STALENESS_MAX_SEG, intervalo_candle_seg=60, agora_ts=None):
    """
    Staleness — descarta a análise se o FECHAMENTO estimado do candle
    mais recente já tem mais de `max_latencia_seg` de idade.

    Correção importante: o timestamp de um candle é a ABERTURA, não o
    fechamento. Um candle M1 recém-aberto sempre tem 0-59s de "idade"
    mesmo em tempo real perfeito — medir direto da abertura reprovava
    quase todo ciclo à toa. Agora soma a duração do candle
    (`intervalo_candle_seg`) antes de comparar.

    `agora_ts` — parâmetro OPCIONAL, só para uso em replay histórico.
    Em produção nunca é passado (fica None) e o comportamento é
    IDÊNTICO ao de sempre: usa time.time() (relógio real). Só quando
    um chamador explicitamente passa agora_ts (ex: replay andando
    candle a candle no passado), a função usa esse timestamp histórico
    no lugar do relógio da máquina — porque comparar time.time() (hoje)
    contra um candle de semanas atrás sempre dava "obsoleto", o que
    inviabilizava qualquer replay sem alterar nenhuma regra de negócio.
    """
    if not candles:
        return True
    fechamento_estimado = candles[-1]['t'] / 1000 + intervalo_candle_seg
    agora = agora_ts if agora_ts is not None else time.time()
    return (agora - fechamento_estimado) > max_latencia_seg


def _find_candle_index_by_timestamp(candles, timestamp):
    if not candles or timestamp is None:
        return None
    for i in range(len(candles) - 1, -1, -1):
        if candles[i].get('t') == timestamp:
            return i
    return None


def _pattern_at_index(candles, idx):
    if not candles or idx is None or idx < 1 or idx >= len(candles):
        return None
    return detect_candle_pattern(candles[:idx + 1])


def _confianca_padrao_candle(padrao, exec_candles, idx=None):
    """Heurística de confiança do padrão de candle (Pin Bar/Hammer), com
    base na proporção pavio/corpo do último candle — NÃO é uma
    probabilidade estatisticamente validada, é um score relativo
    (quanto maior a rejeição, maior a 'confiança' do padrão)."""
    if not padrao or not exec_candles:
        return None
    c = exec_candles[idx if idx is not None else -1]
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
        conn.execute('''
            CREATE TABLE IF NOT EXISTS scalp_gates_vortex_diagnostico (
                pair TEXT,
                categoria TEXT,
                contagem INTEGER DEFAULT 0,
                ultimo_motivo TEXT,
                updated_at INTEGER,
                PRIMARY KEY (pair, categoria)
            )
        ''')
        conn.commit()
        # ── FIX (13/08): mesma causa do fix em init_sfp_liquidez_db —
        # gerenciar_trades_abertos() espera tp/be_movido/parcial_feita
        # em TODA tabela de MODOS_SCALP. Esta tabela só tinha tp1/tp2.
        # "tp" é preenchida no INSERT (ver _save_gates_vortex_signal)
        # com o valor de tp1 (alvo primário, usado pra resolver
        # win/loss e pra BE/parcial). ──
        for alter_sql in [
            "ALTER TABLE scalp_gates_vortex_signal_state ADD COLUMN tp REAL",
            "ALTER TABLE scalp_gates_vortex_signal_state ADD COLUMN be_movido INTEGER DEFAULT 0",
            "ALTER TABLE scalp_gates_vortex_signal_state ADD COLUMN parcial_feita INTEGER DEFAULT 0",
            "ALTER TABLE scalp_gates_vortex_signal_state ADD COLUMN status_gestao TEXT DEFAULT ''",
        ]:
            try:
                conn.execute(alter_sql)
                conn.commit()
            except Exception:
                pass


# ── Classificação do motivo em categoria de gargalo ─────────────────────
# Cada ciclo do gates_vortex termina com um `motivo` (texto livre). Isso
# classifica esse texto num "balde" fixo, pra dar contagem real de ONDE
# o pipeline está travando mais — sem isso, "não veio sinal" é um
# palpite; com isso, vira um número.

_CATEGORIAS_MOTIVO_GATES_VORTEX = [
    ('hora_toxica', 'horário tóxico'),
    ('dados_obsoletos', 'dados obsoletos'),
    ('candles_insuficientes', 'candles insuficientes'),
    ('sem_bias', 'calcular Midnight Open'),
    ('sfp_breakout_cancelado', 'breakout_cancela_analise'),
    ('sem_sfp', 'sem_sfp_ainda'),
    ('sem_sfp', 'sem_liquidez_mapeada'),
    ('sem_sfp', 'sem_candles_sfp'),
    ('padrao_fraco', 'padrão de rejeição fraco'),
    ('sem_candles_mss', 'pra validar MSS'),
    ('sem_mss', 'sem MSS de corpo forte'),
    ('sem_fvg', 'sem FVG real após a expansão'),
    ('risco_invalido', 'risco calculado inválido'),
    ('gates_reprovados', 'falhou nos gates'),
    ('em_cooldown', 'em cooldown'),
]


def _classificar_motivo_gates_vortex(motivo):
    if not motivo:
        return 'sem_motivo'
    if motivo == 'entrada_confirmada':
        return 'sinal_disparado'
    for categoria, chave in _CATEGORIAS_MOTIVO_GATES_VORTEX:
        if chave in motivo:
            return categoria
    return 'outro'


def _garantir_tabela_diagnostico_gates_vortex(db_file):
    """Cria a tabela se ela não existir, sem depender de init_gates_vortex_db
    ter rodado no arranque do app.py — auto-blindado, chamado toda vez que
    a tabela é usada (INSERT ou SELECT)."""
    try:
        with sqlite3.connect(db_file) as conn:
            conn.execute('''
                CREATE TABLE IF NOT EXISTS scalp_gates_vortex_diagnostico (
                    pair TEXT,
                    categoria TEXT,
                    contagem INTEGER DEFAULT 0,
                    ultimo_motivo TEXT,
                    updated_at INTEGER,
                    PRIMARY KEY (pair, categoria)
                )
            ''')
            conn.commit()
    except Exception as e:
        print(f"[scalp_engine gates_vortex] erro ao garantir tabela de diagnóstico: {e}")


def _registrar_diagnostico_gates_vortex(db_file, pair, motivo):
    categoria = _classificar_motivo_gates_vortex(motivo)
    _garantir_tabela_diagnostico_gates_vortex(db_file)
    try:
        with sqlite3.connect(db_file) as conn:
            conn.execute('''
                INSERT INTO scalp_gates_vortex_diagnostico (pair, categoria, contagem, ultimo_motivo, updated_at)
                VALUES (?, ?, 1, ?, ?)
                ON CONFLICT(pair, categoria) DO UPDATE SET
                    contagem = contagem + 1,
                    ultimo_motivo = excluded.ultimo_motivo,
                    updated_at = excluded.updated_at
            ''', (pair, categoria, motivo, int(time.time())))
            conn.commit()
    except Exception as e:
        print(f"[scalp_engine gates_vortex] erro ao registrar diagnóstico de {pair}: {e}")


def diagnostico_gates_vortex_report(db_file, pair=None):
    _garantir_tabela_diagnostico_gates_vortex(db_file)
    try:
        with sqlite3.connect(db_file) as conn:
            cursor = conn.cursor()
            if pair:
                cursor.execute('''
                    SELECT pair, categoria, contagem, ultimo_motivo, updated_at
                    FROM scalp_gates_vortex_diagnostico WHERE pair=?
                ''', (pair,))
            else:
                cursor.execute('''
                    SELECT pair, categoria, contagem, ultimo_motivo, updated_at
                    FROM scalp_gates_vortex_diagnostico
                ''')
            rows = cursor.fetchall()
    except Exception as e:
        return {'erro': str(e)}

    por_par = {}
    total_geral = {}
    for p, categoria, contagem, ultimo_motivo, updated_at in rows:
        por_par.setdefault(p, []).append({
            'categoria': categoria, 'contagem': contagem,
            'ultimo_motivo': ultimo_motivo, 'updated_at': updated_at,
        })
        total_geral[categoria] = total_geral.get(categoria, 0) + contagem

    for p in por_par:
        por_par[p].sort(key=lambda x: x['contagem'], reverse=True)

    ranking_geral = sorted(
        [{'categoria': c, 'contagem': n} for c, n in total_geral.items()],
        key=lambda x: x['contagem'], reverse=True,
    )

    return {'por_par': por_par, 'ranking_geral': ranking_geral}


def _save_gates_vortex_signal(db_file, pair, exec_tf_label, resultado, alerted):
    try:
        signal_id = f"gates_{pair}_{int(time.time()*1000)}"
        with sqlite3.connect(db_file) as conn:
            conn.execute('''
                INSERT INTO scalp_gates_vortex_signal_state
                    (id, pair, created_at, exec_tf, direcao, score, entry, sl, tp, tp1, tp2, alerted)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ''', (
                signal_id, pair, int(time.time()), exec_tf_label,
                resultado['direcao'], resultado['score'],
                resultado['entry'], resultado['sl'], resultado.get('tp1'), resultado['tp1'], resultado['tp2'],
                1 if alerted else 0,
            ))
            conn.commit()
    except Exception as e:
        print(f"[scalp_engine gates_vortex] erro ao salvar sinal de {pair}: {e}")


GATES_VORTEX_EXPIRED_MAX_HOURS = 24


def resolver_expirados_gates_vortex(db_file, pair):
    """
    Estado explícito de ciclo de vida do sinal — antes só existia
    'pendente' -> 'win'/'loss' via gerenciar_trades_abertos (genérico,
    usado por vários modos). Aqui fecha o terceiro estado real: um sinal
    do gates_vortex que passou de GATES_VORTEX_EXPIRED_MAX_HOURS sem
    bater TP nem SL vira 'expired' — não fica pendente pra sempre, e não
    é contabilizado como se ainda estivesse esperando resolução.
    """
    try:
        with sqlite3.connect(db_file) as conn:
            cursor = conn.cursor()
            cursor.execute('''
                SELECT id, created_at FROM scalp_gates_vortex_signal_state
                WHERE pair=? AND alerted=1 AND (resultado_final IS NULL OR resultado_final='pendente')
            ''', (pair,))
            pendentes = cursor.fetchall()

        if not pendentes:
            return

        now = int(time.time())
        max_age_seg = GATES_VORTEX_EXPIRED_MAX_HOURS * 3600
        for signal_id, created_at in pendentes:
            if (now - created_at) > max_age_seg:
                try:
                    with sqlite3.connect(db_file) as conn:
                        conn.execute(
                            "UPDATE scalp_gates_vortex_signal_state SET resultado_final='expired' WHERE id=?",
                            (signal_id,)
                        )
                        conn.commit()
                except Exception as e:
                    print(f"[scalp_engine gates_vortex] erro ao expirar sinal {signal_id}: {e}")
    except Exception as e:
        print(f"[scalp_engine gates_vortex] erro ao checar expirados de {pair}: {e}")


def process_pair_gates_vortex(db_file, pair, candles_por_tf, exec_tf_label='M5', send_telegram_fn=None, agora_ts=None, debug_gates=False):
    """
    Pipeline completo de Gates A-F. Só retorna SIGNAL_DISPARADO quando
    TODOS os 7 gates passarem. Reaproveita Bias/SFP/MSS/FVG do modo
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

    candles_gatilho = candles_m5 or candles_m1
    tf_gatilho_usado = 'M5' if candles_m5 else 'M1'
    intervalo_gatilho_seg = 300 if candles_m5 else 60
    if not candles_gatilho or not candles_h1:
        resultado['motivo'] = 'candles insuficientes (precisa pelo menos H1 e M5/M1)'
        return resultado

    # ── Staleness ──
    if dados_obsoletos(candles_gatilho, intervalo_candle_seg=intervalo_gatilho_seg, agora_ts=agora_ts):
        resultado['motivo'] = f'dados obsoletos — candle mais recente com mais de {GATES_STALENESS_MAX_SEG}s'
        return resultado

    # ── Hora tóxica (só bloqueia XAU/metal — cripto é 24/7, não tem
    # troca de sessão que esvazie liquidez) ──
    if esta_em_hora_toxica_estrita(candles_gatilho, pair=pair):
        resultado['motivo'] = 'horário tóxico (troca de sessão/baixa liquidez) — sinal bloqueado'
        return resultado

    # ── Bias (Midnight Open) ──
    direcao_permitida, midnight_open, bias_context = compute_bias_midnight_open_estrito(pair, candles_por_tf)
    if direcao_permitida is None:
        resultado['motivo'] = 'sem candles suficientes pra calcular Midnight Open'
        return resultado

    # ── SFP contra liquidez (sessão anterior ou 3-7 dias) ──
    liquidez = compute_liquidez_referencia(pair, candles_por_tf)
    sfp, motivo_sfp, tf_sfp_usado, candle_ts_breakout = validar_sfp_cascata_tf(candles_por_tf, liquidez, direcao_permitida)

    # Diagnóstico observacional — roda sempre, não influencia em nada a
    # decisão acima. Só grava fatos objetivos (Casos A-E) pra investigar
    # se o gargalo real é 'nunca tocou' vs 'tocou sem reclaim' vs 'breakout'.
    try:
        tf_diag_label = tf_sfp_usado or 'M15'
        candles_diag = candles_por_tf.get(tf_diag_label) or candles_por_tf.get('M15') or candles_por_tf.get('M5')
        diag = _diagnostico_detalhado_sfp(candles_diag, liquidez, direcao_permitida, tf_diag_label)
        _registrar_diagnostico_sfp(db_file, pair, direcao_permitida, bias_context, diag)
        if debug_gates:
            # ── OBSERVACIONAL PURO — reaproveita o mesmo diag já
            # calculado acima (não recalcula nada), só expõe no dict de
            # retorno pra o replay poder agregar por Caso A-E. ──
            resultado['_debug_sfp_diagnostico'] = {
                'caso': diag.get('caso'),
                'tocou_high_liq': diag.get('tocou_high_liq'), 'tocou_low_liq': diag.get('tocou_low_liq'),
                'fechou_fora_high': diag.get('fechou_fora_high'), 'fechou_fora_low': diag.get('fechou_fora_low'),
                'fechou_de_volta_high': diag.get('fechou_de_volta_high'), 'fechou_de_volta_low': diag.get('fechou_de_volta_low'),
                'candles_analisados': diag.get('candles_analisados'),
                'high_liq': diag.get('high_liq'), 'low_liq': diag.get('low_liq'),
                'cutoff_ts': diag.get('cutoff_ts'), 'tf_usado': tf_diag_label,
                'direcao_permitida': direcao_permitida, 'bias_context': bias_context,
                'motivo_sfp': motivo_sfp,
            }
    except Exception as e:
        print(f"[scalp_engine gates_vortex] erro no diagnóstico observacional de {pair}: {e}")

    # ── AUDITORIA — só grava quando motivo_sfp == 'breakout_cancela_analise'.
    # Não decide nada, não altera nenhuma variável usada pelo restante do
    # pipeline. Fail-open: erro aqui nunca derruba o fluxo principal.
    try:
        if motivo_sfp == 'breakout_cancela_analise':
            _registrar_audit_breakout_cancel(
                db_file, pair,
                cycle_ts=int(time.time()),
                candle_event_ts=candle_ts_breakout,
                bias=bias_context,
                midnight_open=midnight_open,
                high_liq=liquidez.get('high') if liquidez else None,
                low_liq=liquidez.get('low') if liquidez else None,
                motivo=motivo_sfp,
            )
            # ── Chave de dedup pra persistência (patch): transporta os
            # dados já calculados acima (pair, direcao_permitida, candle
            # real do breakout) pra dentro de resultado, sem alterar
            # nenhum valor existente do dict. Consumida só pelo call
            # site único de _registrar_diagnostico_gates_vortex, em
            # process_pair_gates_vortex_com_explicacao().
            resultado['_breakout_cancel_dedup_key'] = {
                'pair': pair,
                'direcao': direcao_permitida,
                'candle_event_ts': candle_ts_breakout,
            }
    except Exception as e:
        print(f"[scalp_engine audit_breakout] erro ao registrar auditoria de {pair}: {e}")

    if not sfp:
        resultado['motivo'] = f'Bias {bias_context}, {motivo_sfp}'
        return resultado
    resultado['tf_sfp_usado'] = tf_sfp_usado

    # ── SFP CAUSAL — bloqueia entrada se este SFP for REPETIDO dentro de
    # um cluster ainda ativo (mesmo evento de liquidez, só mais um
    # candle testando de novo). Só o PRIMEIRO SFP do cluster gera sinal;
    # os repetidos atualizam o estado mas não disparam entrada nem
    # Telegram — evita bombardear com sinais que são o mesmo evento
    # estrutural repetido (auditoria mostrou 92-96% de sobreposição). ──
    try:
        cluster_info = classify_sfp_causal(
            db_file, pair, direcao_permitida,
            event_ts=sfp.get('t'), reference_level=sfp.get('nivel'),
            tf_label=tf_sfp_usado or 'M15',
        )
        resultado['sfp_cluster'] = cluster_info

        # ── TELEMETRIA (13/08) — só coleta, não decide nada. Roda pra
        # TODO SFP confirmado (bloqueado como repetido ou não), porque
        # o objetivo é justamente comparar primeiro vs repetido depois.
        # htf_context e premium_discount_state aqui são cálculos
        # PARALELOS e redundantes aos que os Gates A/G fazem mais
        # adiante — de propósito, pra não compartilhar estado com a
        # decisão real e garantir que travar essa telemetria nunca
        # altera nenhum gate. Reaproveita só as funções já existentes
        # (compute_htf_narrative, compute_premium_discount), sem lógica
        # nova de contexto. ──
        try:
            htf_context_telemetria = compute_htf_narrative(
                candles_d1, candles_por_tf.get('H4'), candles_h1,
            )
        except Exception:
            htf_context_telemetria = None

        premium_discount_telemetria = None
        try:
            pd_ref_telemetria = candles_d1 or candles_h1 or candles_gatilho
            pd_lb_telemetria = min(20, len(pd_ref_telemetria)) if pd_ref_telemetria else 0
            pd_zone_telemetria = compute_premium_discount(pd_ref_telemetria, lookback=pd_lb_telemetria) if pd_ref_telemetria else None
            if pd_zone_telemetria:
                preco_atual_telemetria = candles_gatilho[-1]['c']
                premium_discount_telemetria = 'PREMIUM' if preco_atual_telemetria > pd_zone_telemetria['equilibrium'] else 'DISCOUNT'
        except Exception:
            premium_discount_telemetria = None

        try:
            _registrar_telemetria_sfp(
                db_file, pair, direcao_permitida,
                event_ts=sfp.get('t'), tf_label=tf_sfp_usado or 'M15',
                cluster_info=cluster_info,
                htf_context=htf_context_telemetria,
                premium_discount_state=premium_discount_telemetria,
                bias_context=bias_context,
            )
        except Exception as e:
            print(f"[scalp_engine sfp_telemetria] erro ao registrar {pair}: {e}")

        if cluster_info.get('is_repeated_sfp'):
            resultado['motivo'] = (
                f"SFP repetido dentro do cluster ativo (posição {cluster_info.get('sfp_position')} "
                f"do cluster {cluster_info.get('cluster_id')}) — bloqueado, só o primeiro SFP do "
                f"cluster gera sinal"
            )
            return resultado
    except Exception as e:
        print(f"[scalp_engine gates_vortex] erro no SFP causal de {pair}: {e}")
        # Fail-open: erro na camada de proteção extra não derruba o
        # pipeline principal — segue sem bloqueio de cluster.

    # ── Validação do gatilho de rejeição ────────────────────────────────
    # O padrão precisa pertencer ao EVENTO que gerou a tese (SFP), não ao
    # último candle disponível. Antes, um SFP antigo podia ser confirmado e
    # o engine depois exigir que a vela atual fosse um Hammer/Engolfo, o que
    # eliminava sinais válidos sem relação causal.
    idx_sfp_gatilho = _find_candle_index_by_timestamp(candles_gatilho, sfp.get('t'))
    padrao = _pattern_at_index(candles_gatilho, idx_sfp_gatilho)

    # Se o SFP veio de M15 e o M5 não tiver o timestamp exato, usa a vela
    # mais próxima anterior ao MSS como fallback, sem olhar para o futuro.
    confianca_padrao = _confianca_padrao_candle(padrao, candles_gatilho, idx_sfp_gatilho)
    # O SFP é a confirmação estrutural principal; candle pattern é filtro
    # auxiliar. Aceitamos SFP forte mesmo sem nome clássico de padrão, mas
    # exigimos rejeição mensurável no próprio candle.
    if idx_sfp_gatilho is not None:
        c_sfp = candles_gatilho[idx_sfp_gatilho]
        range_sfp = c_sfp['h'] - c_sfp['l']
        corpo_sfp = abs(c_sfp['c'] - c_sfp['o'])
        pavio_relevante = (c_sfp['h'] - max(c_sfp['o'], c_sfp['c'])) if direcao_permitida == 'baixa' else (min(c_sfp['o'], c_sfp['c']) - c_sfp['l'])
        rejeicao_ok = range_sfp > 0 and pavio_relevante / range_sfp >= 0.25
    else:
        rejeicao_ok = False

    if not rejeicao_ok:
        resultado['motivo'] = f'SFP confirmado, mas rejeição física insuficiente no candle do SFP (padrão={padrao})'
        return resultado

    vols = [c.get('v', 0) for c in candles_gatilho[-20:]]
    media_vol = sum(vols) / len(vols) if vols else 0
    volume_evento = candles_gatilho[idx_sfp_gatilho].get('v', 0) if idx_sfp_gatilho is not None else 0
    volume_ok = volume_evento > media_vol * 1.05 if media_vol else False

    atr_atual = next((v for v in reversed(compute_atr(candles_gatilho, 14)) if v is not None), None)
    idx_atr = idx_sfp_gatilho if idx_sfp_gatilho is not None else len(candles_gatilho) - 1
    atr_evento = compute_atr(candles_gatilho, 14)[idx_atr] if idx_atr < len(candles_gatilho) else None
    range_evento = candles_gatilho[idx_sfp_gatilho]['h'] - candles_gatilho[idx_sfp_gatilho]['l'] if idx_sfp_gatilho is not None else 0
    atr_multiplo = round(range_evento / atr_evento, 2) if atr_evento and atr_evento > 0 else None
    atr_ok = atr_multiplo is not None and atr_multiplo >= 1.05

    # ── MSS no mesmo TF do gatilho (M5 por padrão, igual à Vortex; M1 só se M5 faltar) ──
    candles_mss = candles_m5 or candles_m1
    if not candles_mss:
        resultado['motivo'] = 'SFP e gatilho ok, mas sem candles M5/M1 pra validar MSS'
        return resultado
    mss = validar_mss_m1(candles_mss, sfp, direcao_permitida)
    if not mss:
        resultado['motivo'] = f"SFP confirmado ({sfp['tipo']}), mas sem MSS de corpo forte no {tf_gatilho_usado} ainda"
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

    tp1, tp1_origem = calcular_tp_dinamico(direcao_permitida, entry, sl, candles_m15 or candles_gatilho, candles_d1 or [], min_rr=GATE_E_MIN_RR)
    tp2, tp2_origem = calcular_tp_dinamico(direcao_permitida, entry, sl, candles_m15 or candles_gatilho, candles_d1 or [], min_rr=3.0)
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

    # ── Premium/Discount — princípio SMC básico: não compra caro (Premium),
    # não vende barato (Discount). Diferente do modo '4camadas' (onde isso é
    # só informativo, de propósito), aqui vira veto real (Gate G).
    pd_reference = candles_d1 or candles_h1 or candles_gatilho
    pd_lookback = min(20, len(pd_reference))
    pd_zone = compute_premium_discount(pd_reference, lookback=pd_lookback) if pd_reference else None
    zona_pd = None
    if pd_zone:
        preco_atual_pd = candles_gatilho[-1]['c']
        zona_pd = 'PREMIUM' if preco_atual_pd > pd_zone['equilibrium'] else 'DISCOUNT'

    # ── GATES ──
    gate_a = (det_regime.get('bias_d1') == direcao_permitida or det_regime.get('mtf_alinhado')) and (mss['direcao'] == direcao_permitida)
    gate_b = bool(sfp) and bool(mss) and bool(entry_zone) and atr_ok
    # Monte Carlo é filtro de direção/edge, NÃO uma probabilidade calibrada
    # de win. O valor continua vindo da simulação real do engine.
    gate_c = (not MONTE_CARLO_GATE_ATIVO) or (prob_acerto is not None and prob_acerto >= GATE_C_MONTE_CARLO_MIN_PROB)
    gate_d = smc_quality['obs'] >= GATE_D_MIN_OBS and smc_quality['fvgs'] >= GATE_D_MIN_FVGS
    gate_e = rr_tp1 >= GATE_E_MIN_RR
    gate_f = True  # informativo, nunca bloqueia
    gate_g = True
    if zona_pd == 'PREMIUM' and direcao_permitida == 'alta':
        gate_g = False  # não compra em zona cara
    elif zona_pd == 'DISCOUNT' and direcao_permitida == 'baixa':
        gate_g = False  # não vende em zona barata

    gates = {
        'A_MTF_ALIGNMENT': gate_a,
        'B_TRIGGER': gate_b,
        'C_MONTE_CARLO': gate_c,
        'D_SMC_QUALITY': gate_d,
        'E_MIN_RR': gate_e,
        'F_ICHIMOKU_INFO': gate_f,
        'G_PREMIUM_DISCOUNT': gate_g,
    }
    resultado['gates'] = gates
    resultado['zona_pd'] = zona_pd

    # ── DEBUG opcional (item aprovado do ticket) — OBSERVACIONAL PURO.
    # REGRA ESTRITA: NÃO recombina AND/OR de nenhum gate em lugar
    # nenhum. Só LÊ os operandos crus exatamente como já existem no
    # ponto onde gate_a/gate_b/gate_c são montados logo abaixo (essa
    # leitura acontece DEPOIS de gate_a/gate_b/gate_c já estarem
    # calculados pela produção — resultado_gate_X_real é sempre a
    # MESMA variável gate_X que decide o pipeline, nunca uma cópia
    # recalculada). Cada operando aqui é a mesma variável/expressão
    # atômica (sem AND/OR) que já existe no código de gate_a/gate_b/
    # gate_c logo abaixo — não crio nenhuma condição composta nova.
    # Existe porque resultado['entry']/['sl']/etc. só são escritos no
    # dict público dentro do bloco de SUCESSO (resultado.update({...})
    # mais abaixo) — no branch de gates_reprovados eles ficam None
    # mesmo que os valores locais já tenham sido calculados. Roda só
    # quando debug_gates=True (nunca em produção — fica False por
    # default, resultado idêntico a antes). Só ADICIONA uma chave nova
    # ao dict, não modifica nenhum valor existente nem altera o fluxo
    # de execução. ──
    if debug_gates:
        resultado['_debug_gate_inputs'] = {
            'entry_local': round(entry, 6) if entry is not None else None,
            'sl_local': round(sl, 6) if sl is not None else None,
            'tp1_local': round(tp1, 6) if tp1 is not None else None,
            'tp2_local': round(tp2, 6) if tp2 is not None else None,
            'direcao_local': direcao_permitida,
            'gate_a_operandos': {
                # Operandos ATÔMICOS de gate_a (nenhum AND/OR aqui — só
                # os valores crus que a expressão original usa):
                #   gate_a = (bias_d1==direcao_permitida OR mtf_alinhado) AND (mss['direcao']==direcao_permitida)
                'bias_d1': det_regime.get('bias_d1'),
                'direcao_permitida': direcao_permitida,
                'mtf_alinhado': det_regime.get('mtf_alinhado'),
                'mss_direcao': mss.get('direcao') if mss else None,
                'mss_presente': mss is not None,
                'resultado_gate_a_real': gate_a,  # a MESMA variável que decide o pipeline, não recalculada
            },
            'gate_b_operandos': {
                # Operandos atômicos de gate_b (sem recombinar AND):
                #   gate_b = bool(sfp) and bool(mss) and bool(entry_zone) and atr_ok
                'sfp': sfp, 'mss': mss, 'entry_zone': entry_zone,
                'atr_ok': atr_ok, 'atr_multiplo': round(atr_multiplo, 4) if atr_multiplo is not None else None,
                'resultado_gate_b_real': gate_b,
            },
            'gate_c_operandos': {
                # Operandos atômicos de gate_c (sem recombinar OR/AND):
                #   gate_c = (not MONTE_CARLO_GATE_ATIVO) or (prob_acerto is not None and prob_acerto >= GATE_C_MONTE_CARLO_MIN_PROB)
                'monte_carlo_gate_ativo': MONTE_CARLO_GATE_ATIVO,
                'prob_acerto': prob_acerto,
                'gate_c_min_prob_exigido': GATE_C_MONTE_CARLO_MIN_PROB,
                'resultado_gate_c_real': gate_c,
            },
        }

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
        'tp1_origem': tp1_origem, 'tp2_origem': tp2_origem,
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

    # ── Mensagem SEMPRE montada aqui (mesmo conteúdo rico de antes:
    # tipo de ordem, Edge MC%, TP2, confirmação dos 7 gates), guardada em
    # resultado['telegram_msg'] pra quem chamar decidir se/quando enviar
    # (app.py manda só depois do filtro HTF — ver run_live_cycle). Isso
    # substitui o antigo send_telegram_fn(msg) direto daqui, que mandava
    # ANTES de qualquer filtro de contexto rodar. ──
    arrow = '📈' if direcao_permitida == 'alta' else '📉'
    label = 'COMPRA' if direcao_permitida == 'alta' else 'VENDA'
    tipo_ordem = 'BUY LIMIT' if direcao_permitida == 'alta' else 'SELL LIMIT'
    msg = f"🚦 <b>Gates Vortex — {pair}</b>\n\n"
    msg += f"{arrow} <b>{label}</b> | Score: {score}/100 | Edge MC: {prob_acerto}% (simulação, não calibrada)\n"
    msg += f"📍 <b>Ordem LIMITE ({tipo_ordem}) em {resultado['entry']}</b> — não é a mercado, espera o preço voltar ali (50% do FVG)\n"
    msg += f"🛑 Stop: {resultado['sl']}\n"
    msg += f"✅ TP1: {resultado['tp1']} (RR {resultado['risco_recompensa']})\n"
    if resultado['tp2']:
        msg += f"✅ TP2: {resultado['tp2']}\n"
    msg += "\n<i>Todos os 7 gates confirmados (A-G).</i>"
    resultado['telegram_msg'] = msg

    if send_telegram_fn and not em_cooldown:
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
        'confianca': f"{resultado.get('prob_acerto')}% (Monte Carlo, não calibrado)" if resultado.get('prob_acerto') is not None else None,
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

    wins = losses = pendentes = expirados = 0
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
        elif resultado == 'expired':
            expirados += count
        else:
            pendentes += count

    total_resolvidos = wins + losses
    win_rate = round(100 * wins / total_resolvidos, 1) if total_resolvidos > 0 else None

    total_long = wins_long + losses_long
    total_short = wins_short + losses_short
    win_rate_long = round(100 * wins_long / total_long, 1) if total_long > 0 else None
    win_rate_short = round(100 * wins_short / total_short, 1) if total_short > 0 else None

    return {
        'wins': wins, 'losses': losses, 'pendentes': pendentes, 'expirados': expirados,
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
        # ── FIX (13/08): mesma causa dos fixes acima — esta tabela já
        # tinha "tp", mas faltavam be_movido/parcial_feita/status_gestao,
        # que gerenciar_trades_abertos() usa pra TODAS as tabelas de
        # MODOS_SCALP. ──
        for alter_sql in [
            "ALTER TABLE scalp_4camadas_signal_state ADD COLUMN be_movido INTEGER DEFAULT 0",
            "ALTER TABLE scalp_4camadas_signal_state ADD COLUMN parcial_feita INTEGER DEFAULT 0",
            "ALTER TABLE scalp_4camadas_signal_state ADD COLUMN status_gestao TEXT DEFAULT ''",
        ]:
            try:
                conn.execute(alter_sql)
                conn.commit()
            except Exception:
                pass


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

    # ── Mensagem SEMPRE montada aqui (breakdown completo por camada),
    # guardada em resultado['telegram_msg'] — mesma lógica do
    # gates_vortex acima, quem chama decide se/quando enviar. ──
    arrow = '📈' if direcao_final == 'alta' else '📉'
    label = 'COMPRA' if direcao_final == 'alta' else 'VENDA'
    tipo_ordem = 'BUY LIMIT' if direcao_final == 'alta' else 'SELL LIMIT'
    msg = f"🔶 <b>Sinal 4 Camadas — {pair}</b>\n\n"
    msg += f"{arrow} <b>{label}</b> | TF execução: {exec_tf_label}\n"
    msg += f"📍 <b>Ordem LIMITE ({tipo_ordem}) em {resultado['entry']}</b> — protege contra slippage entre o sinal e a execução\n"
    msg += f"🛑 Stop: {resultado['sl']}\n✅ TP: {resultado['tp']}\n\n"
    msg += f"<b>Score Total: {score_total}/100</b>\n"
    msg += f"• Regime&MTF: {pts_regime}/30 (viés contexto: {det_regime.get('viés_contexto')})\n"
    msg += f"• Estrutura SMC: {pts_estrutura}/30 (estrutura: {det_estrutura.get('estrutura')}, zona: {det_estrutura.get('zona_pd')})\n"
    msg += f"• Gatilho&Energia: {pts_gatilho}/25 (padrão: {det_gatilho.get('padrao_candle')})\n"
    msg += f"• Confluências: {pts_confluencias}/15\n\n"
    msg += "⚠️ <i>Modo experimental — réplica intencional da lógica do concorrente, sem gate de contradição entre camadas.</i>"
    resultado['telegram_msg'] = msg

    if send_telegram_fn and not em_cooldown:
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
            'confianca_pct': prob_acerto_pct,
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




def process_pair_gates_vortex_com_explicacao(db_file, pair, candles_por_tf, exec_tf_label='M5',
                                               send_telegram_fn=None):
    resolver_expirados_gates_vortex(db_file, pair)
    resultado = process_pair_gates_vortex(db_file, pair, candles_por_tf, exec_tf_label, send_telegram_fn)

    # ── DEDUP de persistência para sfp_breakout_cancelado (patch) ──
    # validar_sfp_estrito() continua sendo reavaliada a cada ciclo, sem
    # NENHUMA alteração de lógica. O que muda é só a PERSISTÊNCIA: se o
    # mesmo evento (pair + direcao + candle_event_ts) já foi registrado
    # antes, esta reavaliação não incrementa o contador de novo.
    # Reaproveita o padrão UNIQUE + INSERT OR IGNORE de
    # classify_sfp_causal() / scalp_sfp_cluster_events. Toda categoria
    # que NÃO seja breakout_cancela_analise segue exatamente como antes
    # (chamada incondicional, sem dedup nenhum) — resultado.get(
    # '_breakout_cancel_dedup_key') só existe quando motivo_sfp foi
    # 'breakout_cancela_analise' nesse ciclo.
    dedup_key = resultado.get('_breakout_cancel_dedup_key')
    if dedup_key:
        evento_novo = _evento_breakout_cancel_e_novo(
            db_file, dedup_key['pair'], dedup_key['direcao'], dedup_key['candle_event_ts'],
        )
        if evento_novo:
            _registrar_diagnostico_gates_vortex(db_file, pair, resultado.get('motivo'))
        # se não for evento novo (mesmo candle já registrado antes),
        # pula o registro desta reavaliação — não incrementa de novo.
    else:
        _registrar_diagnostico_gates_vortex(db_file, pair, resultado.get('motivo'))

    exec_candles_ref = candles_por_tf.get('M5') or candles_por_tf.get('M1')
    salvar_explicacao_ultimo_sinal(db_file, 'gates_vortex', pair, resultado, exec_candles=exec_candles_ref)
    return resultado




def build_signal_log(pair, modo_label, htf_narrative, resultado):
    """
    Log explicativo legível (spec seção 23) — usado tanto pro console
    quanto como corpo da mensagem de Telegram. Não duplica nenhum
    detector: só formata o que já está calculado em htf_narrative
    (compute_htf_narrative) e resultado (process_pair_4camadas /
    process_pair_gates_vortex). Não deve ser chamado a cada candle —
    só quando há sinal, bloqueio HTF ou bloqueio de cluster SFP
    (ver call sites em app.py), pra não poluir o log.
    """
    linhas = []
    direcao_raw = resultado.get('direcao')
    direcao_label = 'LONG' if direcao_raw == 'alta' else 'SHORT' if direcao_raw == 'baixa' else None

    bloqueado = bool(resultado.get('bloqueado_por_htf')) or bool(
        (resultado.get('sfp_cluster') or {}).get('is_repeated_sfp')
    )

    if resultado.get('sinal') and not bloqueado:
        emoji = '📈' if direcao_raw == 'alta' else '📉'
        linhas.append(f"🔥 SIGNAL {direcao_label}")
        linhas.append("")
        linhas.append(pair)
        linhas.append(f"Mode: {modo_label}")
        if htf_narrative:
            linhas.append(f"D1: {(htf_narrative.get('d1_bias') or 'neutro').upper()}")
            linhas.append(f"H4: {(htf_narrative.get('h4_bias') or 'neutro').upper()}")
            linhas.append(f"H1: {(htf_narrative.get('h1_bias') or 'neutro').upper()}")
        tf_sfp = resultado.get('tf_sfp_usado')
        if tf_sfp:
            linhas.append(f"{tf_sfp}: SWEEP + CHoCH")
        linhas.append("")
        entry = resultado.get('entry')
        sl = resultado.get('sl')
        tp = resultado.get('tp') or resultado.get('tp1')
        linhas.append(f"Entry: {entry}")
        linhas.append(f"SL: {sl}")
        linhas.append(f"TP: {tp}")
        try:
            if entry is not None and sl is not None and tp is not None and float(entry) != float(sl):
                rr = abs(float(tp) - float(entry)) / abs(float(entry) - float(sl))
                linhas.append(f"RR: {rr:.2f}")
        except (TypeError, ValueError, ZeroDivisionError):
            pass
        linhas.append(f"Score: {resultado.get('score', 0)}/100")
        cluster = resultado.get('sfp_cluster')
        if cluster:
            linhas.append(f"Cluster: {cluster.get('cluster_id')}")
            linhas.append(f"Event: {cluster.get('cluster_last_event')}")
        return "\n".join(linhas)

    # ── Bloqueado ──
    linhas.append(pair)
    if htf_narrative:
        linhas.append(
            f"D1={htf_narrative.get('d1_bias', 'neutro')} "
            f"H4={htf_narrative.get('h4_bias', 'neutro')} "
            f"H1={htf_narrative.get('h1_bias', 'neutro')} "
            f"| bias={htf_narrative.get('bias')} strength={htf_narrative.get('strength')}"
        )
    if resultado.get('bloqueado_por_htf'):
        linhas.append(f"❌ {resultado.get('motivo_bloqueio_htf', 'bloqueado pelo contexto HTF')}")
    elif resultado.get('sfp_cluster', {}).get('is_repeated_sfp'):
        cluster = resultado['sfp_cluster']
        linhas.append(f"❌ SFP repetido (posição {cluster.get('sfp_position')} do cluster {cluster.get('cluster_id')})")
    elif resultado.get('motivo'):
        linhas.append(f"⏳ {resultado.get('motivo')}")
    else:
        linhas.append("⏳ aguardando")
    return "\n".join(linhas)


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


@explicacao_bp.route("/scalp_gates_vortex/diagnostico", methods=["GET"])
def diagnostico_gates_vortex():
    """
    Mostra ONDE o pipeline do gates_vortex está travando mais, por par e
    no geral — contagem real, não achismo. 'pair' opcional na query
    string filtra por um par só (ex: ?pair=BTCUSD).
    """
    pair = request.args.get('pair')
    try:
        report = diagnostico_gates_vortex_report(_db_file_explicacao(), pair=pair)
    except Exception as e:
        return jsonify({"erro": str(e)}), 500
    return jsonify(report)


@explicacao_bp.route("/scalp_gates_vortex/sfp_telemetria", methods=["GET"])
def sfp_telemetria_endpoint():
    """
    Telemetria crua de cada SFP confirmado (bloqueado como repetido ou
    não) — cluster_id, sfp_position, cluster_size_so_far,
    candles_since_first_sfp/previous_sfp, contexto HTF e premium/
    discount no momento do evento. Só coleta, não influencia nenhuma
    decisão do gates_vortex (ver comentário em process_pair_gates_vortex).
    'pair' opcional filtra 1 par. 'limit' controla quantos registros
    (default 200, mais recentes primeiro).
    """
    pair = request.args.get('pair')
    limit = int(request.args.get('limit', 200))
    try:
        report = sfp_telemetria_report(_db_file_explicacao(), pair=pair, limit=limit)
    except Exception as e:
        return jsonify({"erro": str(e)}), 500
    return jsonify(report)


@explicacao_bp.route("/scalp_gates_vortex/diagnostico_sfp", methods=["GET"])
def diagnostico_sfp_gates_vortex():
    """
    Estado mais recente e detalhado da análise de SFP por par: liquidez
    calculada, se tocou, se fechou fora, se voltou, candle responsável.
    Responde objetivamente os Casos A-E (nunca tocou / tocou sem reclaim /
    breakout / SFP confirmado / sem dados). 'pair' opcional filtra 1 par.
    """
    pair = request.args.get('pair')
    try:
        report = sfp_diagnostico_report(_db_file_explicacao(), pair=pair)
    except Exception as e:
        return jsonify({"erro": str(e)}), 500
    return jsonify(report)


def audit_breakout_cancel_report(db_file):
    """
    Roda as 4 análises combinadas sobre audit_breakout_cancel:
    1) total / únicos / fator de repetição (visão geral)
    2) por pair
    3) por bias
    4) top candles mais repetidos (candle_event_ts com >1 ocorrência)

    Só leitura. Não altera nada, não decide nada, não influencia o
    pipeline. Se a tabela ainda não existir (nenhum breakout_cancela_analise
    ocorreu ainda desde o deploy), devolve zeros em vez de erro.
    """
    _garantir_tabela_audit_breakout_cancel(db_file)
    try:
        with sqlite3.connect(db_file) as conn:
            cursor = conn.cursor()

            # 1) Visão geral
            cursor.execute('''
                SELECT COUNT(*), COUNT(DISTINCT candle_event_ts)
                FROM audit_breakout_cancel
            ''')
            total, unicos = cursor.fetchone()
            fator_repeticao = round(total / unicos, 2) if unicos else None

            # 2) Por pair
            cursor.execute('''
                SELECT pair, COUNT(*) AS total, COUNT(DISTINCT candle_event_ts) AS unicos
                FROM audit_breakout_cancel
                GROUP BY pair ORDER BY total DESC
            ''')
            por_pair = [
                {
                    'pair': p, 'total': t, 'unicos': u,
                    'fator_repeticao': round(t / u, 2) if u else None,
                }
                for p, t, u in cursor.fetchall()
            ]

            # 3) Por bias
            cursor.execute('''
                SELECT bias, COUNT(*) AS total, COUNT(DISTINCT candle_event_ts) AS unicos
                FROM audit_breakout_cancel
                GROUP BY bias
            ''')
            por_bias = [
                {
                    'bias': b, 'total': t, 'unicos': u,
                    'fator_repeticao': round(t / u, 2) if u else None,
                }
                for b, t, u in cursor.fetchall()
            ]

            # 4) Top candles mais repetidos
            cursor.execute('''
                SELECT candle_event_ts, pair, COUNT(*) AS ocorrencias,
                       MIN(cycle_ts) AS primeiro_ciclo, MAX(cycle_ts) AS ultimo_ciclo
                FROM audit_breakout_cancel
                GROUP BY candle_event_ts, pair
                HAVING COUNT(*) > 1
                ORDER BY ocorrencias DESC
                LIMIT 20
            ''')
            top_repetidos = [
                {
                    'candle_event_ts': ts, 'pair': p, 'ocorrencias': n,
                    'primeiro_ciclo': primeiro, 'ultimo_ciclo': ultimo,
                    'intervalo_segundos': (ultimo - primeiro) if (primeiro is not None and ultimo is not None) else None,
                }
                for ts, p, n, primeiro, ultimo in cursor.fetchall()
            ]

    except Exception as e:
        return {'erro': str(e)}

    # Conclusão automática, só como referência rápida — não substitui
    # análise manual, mas já indica pra qual lado os números apontam.
    conclusao = 'C_INCONCLUSIVO_DADOS_INSUFICIENTES'
    if total and total >= 20:
        if fator_repeticao is not None:
            if fator_repeticao >= 3:
                conclusao = 'A_PROVAVEL_TELEMETRIA_INFLACIONADA'
            elif fator_repeticao <= 1.3:
                conclusao = 'B_PROVAVEL_EVENTOS_REAIS'
            else:
                conclusao = 'C_INCONCLUSIVO_ZONA_CINZENTA'

    return {
        'visao_geral': {
            'total_ocorrencias': total,
            'candle_event_ts_unicos': unicos,
            'fator_repeticao': fator_repeticao,
        },
        'por_pair': por_pair,
        'por_bias': por_bias,
        'top_candles_repetidos': top_repetidos,
        'conclusao_automatica_referencial': conclusao,
        'nota': (
            'Conclusão automática é só um indicador rápido baseado em fator_repeticao '
            '(>=3 sugere A, <=1.3 sugere B, entre os dois é zona cinzenta C). '
            'A leitura final continua sendo manual, olhando top_candles_repetidos e '
            'o contexto de cada pair/bias.'
        ),
    }


@explicacao_bp.route("/scalp_gates_vortex/audit_breakout_report", methods=["GET"])
def audit_breakout_report_endpoint():
    """
    Endpoint de leitura da tabela audit_breakout_cancel — consolida as
    4 análises combinadas (total/únicos/fator, por pair, por bias, top
    candles repetidos) num único JSON. Só leitura, não decide nada, não
    influencia o pipeline de produção.
    Uso: GET /scalp_gates_vortex/audit_breakout_report
    """
    try:
        report = audit_breakout_cancel_report(_db_file_explicacao())
    except Exception as e:
        return jsonify({"erro": str(e)}), 500
    return jsonify(report)


# ═══════════════════════════════════════════════════════════════════════
# REPLAY HISTÓRICO — ferramenta de análise, NÃO faz parte do pipeline de
# produção. Não é chamado por nenhum ciclo automático, não influencia
# nenhum sinal real. Só roda quando alguém acessa o endpoint manualmente.
# Objetivo: transformar a hipótese "janela de 7 dias é ampla demais" em
# evidência, comparando 4 referências de liquidez lado a lado.
# ═══════════════════════════════════════════════════════════════════════

def _classificar_periodo_liquidez(candles_periodo, high_liq, low_liq, direcao_permitida):
    """
    Distingue explicitamente TOUCH, SWEEP, RECLAIM, SFP, BREAKOUT e
    NUNCA_TOCOU — sem misturar os conceitos, exatamente como pedido:

    TOUCH:   high >= high_liq   OU   low <= low_liq   (encostou, com igualdade)
    SWEEP:   high >  high_liq   OU   low  <  low_liq   (ultrapassou de fato)
    RECLAIM: depois do sweep, o MESMO candle fecha de volta dentro
    SFP:     SWEEP + RECLAIM no mesmo candle fechado
    BREAKOUT: SWEEP sem RECLAIM (fechou fora, ficou fora)
    NUNCA_TOCOU: nenhum TOUCH em todo o período analisado
    """
    lado = 'high' if direcao_permitida == 'baixa' else 'low'
    nivel = high_liq if lado == 'high' else low_liq

    houve_touch = False
    for c in candles_periodo:
        if lado == 'high':
            if c['h'] >= nivel:
                houve_touch = True
            if c['h'] > nivel:  # SWEEP
                if c['c'] > nivel:
                    return 'BREAKOUT', c
                else:
                    return 'SFP', c
        else:
            if c['l'] <= nivel:
                houve_touch = True
            if c['l'] < nivel:  # SWEEP
                if c['c'] < nivel:
                    return 'BREAKOUT', c
                else:
                    return 'SFP', c

    return ('TOUCH_SEM_SWEEP' if houve_touch else 'NUNCA_TOCOU'), None


def _janela_liquidez_7d(d1_ate_aqui):
    janela = d1_ate_aqui[-8:-1] if len(d1_ate_aqui) >= 8 else d1_ate_aqui[:-1]
    if len(janela) < 3:
        return None
    return {'high': max(c['h'] for c in janela), 'low': min(c['l'] for c in janela)}


def _janela_liquidez_nd(d1_ate_aqui, n_dias):
    janela = d1_ate_aqui[-(n_dias + 1):-1] if len(d1_ate_aqui) >= (n_dias + 1) else d1_ate_aqui[:-1]
    if len(janela) < 1:
        return None
    return {'high': max(c['h'] for c in janela), 'low': min(c['l'] for c in janela)}


def _janela_liquidez_swing_d1(d1_ate_aqui, swing_size=10):
    """Swing estrutural (não é só range bruto) — usa o algoritmo de swings
    que já existe no engine (_extrair_swings_lux_algo), pega o swing high
    e swing low mais recentes antes do ponto atual."""
    if len(d1_ate_aqui) < swing_size + 5:
        return None
    swings = _extrair_swings_lux_algo(d1_ate_aqui, swing_size=swing_size)
    if not swings:
        return None
    highs = [s['valor'] for s in swings if s['tipo'] == 'high']
    lows = [s['valor'] for s in swings if s['tipo'] == 'low']
    if not highs or not lows:
        return None
    return {'high': highs[-1], 'low': lows[-1]}


REFERENCIAS_LIQUIDEZ = {
    '7D': _janela_liquidez_7d,
    '3D': lambda d1: _janela_liquidez_nd(d1, 3),
    '1D': lambda d1: _janela_liquidez_nd(d1, 1),
    'SWING_D1': _janela_liquidez_swing_d1,
}


INTERVALO_MS_POR_LABEL = {'D': 86400000, '15': 900000, '60': 3600000, '5': 300000}


def _remover_candle_em_formacao(candles, interval_label):
    """
    Remove o último candle se ele ainda estiver em formação — checado por
    timestamp + horário atual, não por 'a API parece já ter fechado'.
    Um candle com timestamp de abertura `t` só está fechado se
    `t + duração_do_candle <= agora`.
    """
    if not candles:
        return candles, False
    intervalo_ms = INTERVALO_MS_POR_LABEL.get(interval_label, 900000)
    agora_ms = int(time.time() * 1000)
    ultimo = candles[-1]
    if ultimo['t'] + intervalo_ms > agora_ms:
        return candles[:-1], True
    return candles, False


def _deduplicar_e_ordenar_candles(candles):
    """Deduplica por timestamp (defensivo — a paginação já deveria evitar
    isso, mas não confiamos só nisso) e ordena cronologicamente."""
    vistos = {}
    for c in candles:
        vistos[c['t']] = c  # último visto vence, mas timestamps de kline não deveriam repetir com valores diferentes
    candles_unicos = list(vistos.values())
    candles_unicos.sort(key=lambda c: c['t'])
    n_duplicados_removidos = len(candles) - len(candles_unicos)
    return candles_unicos, n_duplicados_removidos


def _detectar_gaps(candles, interval_label):
    """
    Detecta buracos na série (ex: 10:00, 10:15, 11:00 — faltou o de
    10:30). NÃO inventa candle, NÃO descarta o dataset — só registra.
    """
    intervalo_esperado = INTERVALO_MS_POR_LABEL.get(interval_label, 900000)
    gaps = []
    for i in range(1, len(candles)):
        delta = candles[i]['t'] - candles[i - 1]['t']
        if delta > intervalo_esperado:
            gaps.append({
                'apos_ts': candles[i - 1]['t'], 'antes_ts': candles[i]['t'],
                'delta_minutos': round(delta / 60000, 1),
            })
    maior_gap = max((g['delta_minutos'] for g in gaps), default=0)
    return {
        'numero_de_gaps': len(gaps),
        'maior_gap_minutos': maior_gap,
        'gaps_principais': sorted(gaps, key=lambda g: g['delta_minutos'], reverse=True)[:10],
    }


def _validar_e_limpar_candles(candles_brutos, interval_label):
    """
    Pipeline completo, na ordem pedida:
    raw -> remove incompleto -> dedup -> ordena -> valida timestamps -> detecta gaps
    """
    relatorio = {'candles_brutos': len(candles_brutos)}

    candles_sem_forming, removeu_forming = _remover_candle_em_formacao(candles_brutos, interval_label)
    relatorio['candle_em_formacao_removido'] = removeu_forming

    candles_limpos, n_dup = _deduplicar_e_ordenar_candles(candles_sem_forming)
    relatorio['duplicados_removidos'] = n_dup

    timestamps_validos = all(isinstance(c.get('t'), int) and c['t'] > 0 for c in candles_limpos)
    relatorio['timestamps_validos'] = timestamps_validos

    relatorio['gaps'] = _detectar_gaps(candles_limpos, interval_label)
    relatorio['candles_finais'] = len(candles_limpos)

    return candles_limpos, relatorio



def _fetch_bybit_klines_historico(symbol, interval, dias_historico):
    """
    Busca candles históricos direto da Bybit V5, com paginação (a API
    limita a 1000 candles por request). Só usado pelo replay — nunca
    pelo pipeline de produção, que recebe candles já prontos de fora.
    """
    intervalo_ms = {'D': 86400000, '15': 900000, '60': 3600000, '5': 300000}.get(interval, 900000)
    total_candles_necessarios = int((dias_historico * 86400000) / intervalo_ms) + 20
    todos = []
    end_ts = None

    while len(todos) < total_candles_necessarios:
        url = f'https://api.bybit.com/v5/market/kline?category=linear&symbol={symbol}&interval={interval}&limit=1000'
        if end_ts:
            url += f'&end={end_ts}'
        try:
            resp = requests.get(url, timeout=10)
            resp.raise_for_status()
            data = resp.json()
            lista = data.get('result', {}).get('list', [])
        except Exception as e:
            print(f"[replay] erro ao buscar candles de {symbol} ({interval}): {e}")
            break
        if not lista:
            break
        candles_pagina = [
            {'t': int(k[0]), 'o': float(k[1]), 'h': float(k[2]), 'l': float(k[3]), 'c': float(k[4]), 'v': float(k[5]) if len(k) > 5 else 0}
            for k in lista
        ]
        candles_pagina.sort(key=lambda c: c['t'])
        todos = candles_pagina + todos
        if len(candles_pagina) < 1000:
            break
        end_ts = candles_pagina[0]['t'] - 1

    todos.sort(key=lambda c: c['t'])
    return todos


HORIZONTES_CANDLES = [1, 3, 5, 10, 20]
NIVEIS_ALVO_FAVORAVEL_PCT = [0.25, 0.50, 0.75, 1.00, 1.50, 2.00]
NIVEIS_ALVO_ADVERSO_PCT = [0.25, 0.50, 0.75, 1.00]
CENARIOS_RR_FIXOS = [(0.5, 0.5), (0.75, 0.5), (1.0, 0.5), (1.5, 0.75), (2.0, 1.0)]  # (tp_pct, sl_pct)
RR_MAX_LOOKAHEAD_CANDLES = 50


def _medir_movimento_durante_candle(candle_evento, direcao_permitida, entry):
    """
    Investigação 1 — mede o MFE/MAE que já aconteceu DENTRO do próprio
    candle do SFP (do open até o extremo do candle), ANTES mesmo do close
    (que é o ENTRY). Serve só pra comparar com o MFE/MAE pós-entry — não
    muda ENTRY, não muda nada da decisão. Testa a hipótese: "o candle SFP
    já consumiu boa parte do movimento favorável antes do close acontecer".
    """
    open_candle = candle_evento['o']
    if direcao_permitida == 'baixa':
        mfe_durante = max(0, open_candle - candle_evento['l'])
        mae_durante = max(0, candle_evento['h'] - open_candle)
    else:
        mfe_durante = max(0, candle_evento['h'] - open_candle)
        mae_durante = max(0, open_candle - candle_evento['l'])
    return {
        'mfe_durante_pct': round(mfe_durante / entry * 100, 4),
        'mae_durante_pct': round(mae_durante / entry * 100, 4),
    }


def _avaliar_qualidade_sfp_evento(m15, idx_evento, direcao_permitida, entry):
    """
    Fase 2 — mede o comportamento REAL do preço depois de 1 SFP confirmado:
    - MFE/MAE em vários horizontes (1/3/5/10/20 candles à frente)
    - Cenários de TP/SL fixos: qual bate primeiro, candle a candle
    - Se TP e SL forem tocados no MESMO candle, não dá pra saber a ordem
      intrabar de verdade — marca AMBIGUO em vez de inventar um resultado
    """
    max_horizonte = max(HORIZONTES_CANDLES)
    janela_max = m15[idx_evento + 1: idx_evento + 1 + max(max_horizonte, RR_MAX_LOOKAHEAD_CANDLES)]
    if not janela_max:
        return None

    horizontes_resultado = {}
    for h in HORIZONTES_CANDLES:
        janela_h = janela_max[:h]
        if not janela_h:
            horizontes_resultado[h] = None
            continue
        if direcao_permitida == 'baixa':
            mfe = max(0, entry - min(c['l'] for c in janela_h))
            mae = max(0, max(c['h'] for c in janela_h) - entry)
        else:
            mfe = max(0, max(c['h'] for c in janela_h) - entry)
            mae = max(0, entry - min(c['l'] for c in janela_h))
        horizontes_resultado[h] = {
            'mfe_pct': round(mfe / entry * 100, 4),
            'mae_pct': round(mae / entry * 100, 4),
        }

    cenarios_resultado = {}
    for tp_pct, sl_pct in CENARIOS_RR_FIXOS:
        if direcao_permitida == 'baixa':
            tp_nivel = entry * (1 - tp_pct / 100)
            sl_nivel = entry * (1 + sl_pct / 100)
        else:
            tp_nivel = entry * (1 + tp_pct / 100)
            sl_nivel = entry * (1 - sl_pct / 100)

        outcome = 'NENHUM'
        for c in janela_max[:RR_MAX_LOOKAHEAD_CANDLES]:
            if direcao_permitida == 'baixa':
                bateu_tp, bateu_sl = c['l'] <= tp_nivel, c['h'] >= sl_nivel
            else:
                bateu_tp, bateu_sl = c['h'] >= tp_nivel, c['l'] <= sl_nivel
            if bateu_tp and bateu_sl:
                outcome = 'AMBIGUO'
                break
            elif bateu_tp:
                outcome = 'TP'
                break
            elif bateu_sl:
                outcome = 'SL'
                break
        cenarios_resultado[f'{tp_pct}/{sl_pct}'] = outcome

    return {'horizontes': horizontes_resultado, 'cenarios_rr': cenarios_resultado}


def _percentil(valores_ordenados, p):
    if not valores_ordenados:
        return None
    idx = int(len(valores_ordenados) * p / 100)
    idx = min(idx, len(valores_ordenados) - 1)
    return round(valores_ordenados[idx], 4)



def _qualidade_primeiro_vs_repetido(eventos_sfp):
    """
    SEGUNDO da investigação — separa PRIMEIRO_SFP (evento independente,
    abre um cluster novo) de SFP_REPETIDO (continuação de um cluster já
    em andamento), e calcula a MESMA agregação de qualidade
    (_agregar_qualidade_fase2, sem alterar) só nos primeiros. Isso testa
    o Cenário A da hipótese: será que só os repetidos são ruins?
    """
    marcados = _marcar_primeiro_ou_repetido(eventos_sfp)
    primeiros = [e for e in marcados if e['posicao_no_cluster'] == 'primeiro']
    repetidos = [e for e in marcados if e['posicao_no_cluster'] == 'repetido']
    return {
        'primeiro_sfp': _agregar_qualidade_fase2(primeiros),
        'sfp_repetido': _agregar_qualidade_fase2(repetidos),
    }


def _qualidade_por_direcao(eventos_sfp):
    """QUARTO da investigação — separa LONG (alta) de SHORT (baixa),
    mesma agregação já existente, sem lógica nova."""
    longs = [e for e in eventos_sfp if e.get('direcao') == 'alta']
    shorts = [e for e in eventos_sfp if e.get('direcao') == 'baixa']
    return {
        'LONG': _agregar_qualidade_fase2(longs),
        'SHORT': _agregar_qualidade_fase2(shorts),
    }


def _comparar_movimento_intra_vs_pos_entry(eventos_sfp, horizonte_referencia=20):
    """
    Monta a tabela pedida na Investigação 1: MFE/MAE que já aconteceu
    DENTRO do candle SFP (antes do close/entry) vs MFE/MAE medido DEPOIS
    do entry (horizonte de referência, default 20 candles). Testa a
    hipótese "o candle já consumiu o movimento antes da entrada real".
    """
    if not eventos_sfp:
        return {'total_sfp': 0}

    mfe_durante = sorted(e['mfe_durante_pct'] for e in eventos_sfp if 'mfe_durante_pct' in e)
    mae_durante = sorted(e['mae_durante_pct'] for e in eventos_sfp if 'mae_durante_pct' in e)
    mfe_pos = sorted(e['horizontes'][horizonte_referencia]['mfe_pct'] for e in eventos_sfp if e.get('horizontes', {}).get(horizonte_referencia))
    mae_pos = sorted(e['horizontes'][horizonte_referencia]['mae_pct'] for e in eventos_sfp if e.get('horizontes', {}).get(horizonte_referencia))

    def media(lista):
        return round(sum(lista) / len(lista), 4) if lista else None

    def com_percentis(lista):
        return {
            'medio': media(lista), 'p25': _percentil(lista, 25),
            'p50': _percentil(lista, 50), 'p75': _percentil(lista, 75),
        }

    return {
        'total_sfp': len(eventos_sfp),
        'horizonte_referencia_candles': horizonte_referencia,
        'mfe_durante_sfp': com_percentis(mfe_durante),
        'mae_durante_sfp': com_percentis(mae_durante),
        'mfe_pos_entry': com_percentis(mfe_pos),
        'mae_pos_entry': com_percentis(mae_pos),
        'mfe_durante_sfp_medio_pct': media(mfe_durante),
        'mae_durante_sfp_medio_pct': media(mae_durante),
        'mfe_pos_entry_medio_pct': media(mfe_pos),
        'mae_pos_entry_medio_pct': media(mae_pos),
    }


def _marcar_primeiro_ou_repetido(eventos_sfp, horizonte_candles=20, intervalo_ms=900000):
    """
    Reaproveita EXATAMENTE a mesma regra causal do _agrupar_clusters_sfp
    (mesma janela, mesmo encadeamento) só pra marcar cada evento como
    'primeiro' (abre um cluster novo) ou 'repetido' (continua um cluster
    já em andamento). Não é lógica nova — é a mesma decisão de
    clustering, só devolvendo a lista de eventos com uma tag a mais.
    """
    if not eventos_sfp:
        return []
    eventos_ordenados = sorted(eventos_sfp, key=lambda e: e['timestamp'])
    janela_ms = horizonte_candles * intervalo_ms
    marcados = []
    ultimo_do_cluster_atual = None
    for e in eventos_ordenados:
        if ultimo_do_cluster_atual is None or (e['timestamp'] - ultimo_do_cluster_atual) > janela_ms:
            marcados.append({**e, 'posicao_no_cluster': 'primeiro'})
        else:
            marcados.append({**e, 'posicao_no_cluster': 'repetido'})
        ultimo_do_cluster_atual = e['timestamp']
    return marcados


def _agrupar_clusters_sfp(eventos_sfp, horizonte_candles=20, intervalo_ms=900000):
    """
    Investigação 2 — CLUSTER_CAUSAL (confirmado com teste, não é retrospectivo):
    cada evento só é comparado com o ÚLTIMO evento já incluído no cluster
    atual (já ocorrido no passado) — encadeamento sequencial. Provado que
    processar os eventos 1 por 1 (como chegariam ao vivo) dá o MESMO
    resultado que processar tudo de uma vez. Não remove nenhum evento
    original, só reorganiza pra análise.

    Distância máxima entre SFPs no mesmo cluster: horizonte_candles (20).
    Cluster encerra quando aparece um evento além dessa distância do
    último membro — só se sabe isso quando esse próximo evento chega.
    """
    if not eventos_sfp:
        return {'total_clusters': 0, 'sfp_por_cluster_medio': None,
                'sfp_por_cluster_mediana': None, 'maior_cluster': 0,
                'primeiro_sfp_do_cluster': 0, 'sfp_repetido': 0, 'clusters': []}

    eventos_ordenados = sorted(eventos_sfp, key=lambda e: e['timestamp'])
    janela_ms = horizonte_candles * intervalo_ms
    clusters = []
    cluster_atual = [eventos_ordenados[0]]

    for e in eventos_ordenados[1:]:
        ultimo_no_cluster = cluster_atual[-1]
        if e['timestamp'] - ultimo_no_cluster['timestamp'] <= janela_ms:
            cluster_atual.append(e)
        else:
            clusters.append(cluster_atual)
            cluster_atual = [e]
    clusters.append(cluster_atual)

    tamanhos = [len(c) for c in clusters]
    tamanhos_ordenados = sorted(tamanhos)
    mediana = tamanhos_ordenados[len(tamanhos_ordenados) // 2] if tamanhos_ordenados else None

    clusters_resumo = []
    for c in clusters:
        direcoes = list(set(e['direcao'] for e in c))
        clusters_resumo.append({
            'primeiro_sfp_ts': c[0]['timestamp'], 'ultimo_sfp_ts': c[-1]['timestamp'],
            'tamanho': len(c),
            'direcao': direcoes[0] if len(direcoes) == 1 else 'mista',
        })

    total_sfp = len(eventos_ordenados)
    primeiro_sfp_do_cluster = len(clusters)  # cada cluster tem exatamente 1 "primeiro"
    sfp_repetido = total_sfp - primeiro_sfp_do_cluster  # o resto são continuações do mesmo cluster

    return {
        'metodologia': 'CLUSTER_CAUSAL — cada decisão usa só o último evento já ocorrido, '
                        'confirmado com teste (streaming == batch). Distância máxima: '
                        f'{horizonte_candles} candles do último membro do cluster.',
        'total_clusters': len(clusters),
        'sfp_por_cluster_medio': round(sum(tamanhos) / len(tamanhos), 2) if tamanhos else None,
        'sfp_por_cluster_mediana': mediana,
        'maior_cluster': max(tamanhos) if tamanhos else 0,
        'primeiro_sfp_do_cluster': primeiro_sfp_do_cluster,
        'sfp_repetido': sfp_repetido,
        'clusters': clusters_resumo,
    }


# ── SFP CAUSAL EM TEMPO REAL — bloqueia entrada em SFP repetido dentro
# do cluster ativo. Reaproveita _marcar_primeiro_ou_repetido/
# _agrupar_clusters_sfp (mesma regra já auditada: cada evento só compara
# com o ÚLTIMO evento já ocorrido do mesmo par+direção, distância máxima
# de 20 candles) — só adiciona persistência em DB pra funcionar entre
# ciclos live (o histórico não cabe inteiro em memória a cada chamada). ──

TF_LABEL_INTERVALO_MS = {
    'D1': 86400000, 'H4': 14400000, 'H1': 3600000,
    'M15': 900000, 'M5': 300000, 'M1': 60000,
}


def init_sfp_cluster_db(db_file):
    try:
        with sqlite3.connect(db_file) as conn:
            conn.execute('''
                CREATE TABLE IF NOT EXISTS scalp_sfp_cluster_events (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    pair TEXT NOT NULL,
                    direcao TEXT NOT NULL,
                    timestamp INTEGER NOT NULL,
                    reference_level REAL,
                    created_at INTEGER,
                    UNIQUE(pair, direcao, timestamp)
                )
            ''')
            conn.execute('''
                CREATE INDEX IF NOT EXISTS idx_sfp_cluster_pair_dir_ts
                ON scalp_sfp_cluster_events(pair, direcao, timestamp)
            ''')
            conn.commit()
    except Exception as e:
        print(f"[scalp_engine sfp_cluster] erro ao criar tabela: {e}")


def classify_sfp_causal(db_file, pair, direcao, event_ts, reference_level=None, tf_label='M15'):
    """
    Classifica um evento de SFP como PRIMEIRO ou REPETIDO do cluster,
    de forma 100% causal — só usa eventos já persistidos com
    timestamp <= event_ts, nunca olha pra frente.

    Idempotente: reprocessar o MESMO event_ts (o mesmo SFP detectado de
    novo em ciclos live seguintes, antes do preço se mover) devolve
    sempre a mesma classificação, sem inflar o cluster.

    Retorna dict com cluster_id, is_first_sfp, is_repeated_sfp,
    sfp_position (posição do evento dentro do cluster, 1-based),
    cluster_start, cluster_last_event, total_eventos_pair_direcao.

    Campos de TELEMETRIA (13/08, markup "SFP causal — instrumentação"):
    candles_since_first_sfp, candles_since_previous_sfp e
    cluster_size_so_far — puramente aditivos, calculados em cima do
    MESMO encadeamento causal já usado pra decidir is_first/is_repeated
    acima. Não influenciam is_first_sfp/is_repeated_sfp nem nenhuma
    outra decisão — é reaproveitamento de dado que a função já calcula
    (marcados, cluster_start, sfp_position), só exposto pra quem quiser
    registrar/analisar depois.
    """
    init_sfp_cluster_db(db_file)
    intervalo_ms = TF_LABEL_INTERVALO_MS.get(tf_label, 900000)

    try:
        with sqlite3.connect(db_file) as conn:
            conn.execute('''
                INSERT OR IGNORE INTO scalp_sfp_cluster_events
                    (pair, direcao, timestamp, reference_level, created_at)
                VALUES (?, ?, ?, ?, ?)
            ''', (pair, direcao, event_ts, reference_level, int(time.time())))
            conn.commit()

            cursor = conn.cursor()
            cursor.execute('''
                SELECT timestamp FROM scalp_sfp_cluster_events
                WHERE pair=? AND direcao=? AND timestamp<=?
                ORDER BY timestamp ASC
            ''', (pair, direcao, event_ts))
            rows = cursor.fetchall()
    except Exception as e:
        print(f"[scalp_engine sfp_cluster] erro ao classificar {pair}/{direcao}: {e}")
        # Fail-open: se o DB falhar, trata como primeiro (não bloqueia
        # o sistema por causa de uma camada de proteção extra).
        return {
            'cluster_id': None, 'is_first_sfp': True, 'is_repeated_sfp': False,
            'sfp_position': 1, 'cluster_start': event_ts, 'cluster_last_event': event_ts,
            'total_eventos_pair_direcao': 1, 'erro': str(e),
        }

    eventos = [{'timestamp': ts, 'direcao': direcao} for (ts,) in rows]
    marcados = _marcar_primeiro_ou_repetido(eventos, horizonte_candles=20, intervalo_ms=intervalo_ms)

    # marcados está ordenado por timestamp; o evento atual é o último
    # (já que filtramos timestamp<=event_ts e ele é o próprio máximo).
    atual = marcados[-1]
    is_first = atual['posicao_no_cluster'] == 'primeiro'

    # Reconstrói o cluster do evento atual (mesma regra de encadeamento)
    # pra achar cluster_start e sfp_position.
    cluster_start = event_ts
    sfp_position = 1
    for i in range(len(marcados) - 1, -1, -1):
        if marcados[i]['posicao_no_cluster'] == 'primeiro':
            cluster_start = marcados[i]['timestamp']
            sfp_position = len(marcados) - i
            break

    cluster_id = f"{pair}_{direcao}_{cluster_start}"

    # ── Telemetria adicional (não decide nada, só descreve) ──
    # candles_since_first_sfp: distância (em candles do tf_label) entre
    # o evento atual e o início do cluster a que ele pertence.
    candles_since_first_sfp = round((event_ts - cluster_start) / intervalo_ms, 2)

    # candles_since_previous_sfp: distância até o evento imediatamente
    # anterior JÁ PERSISTIDO (marcados[-2], se existir) — None se este
    # for o primeiro evento já visto pra esse pair+direção (não só o
    # primeiro do cluster, o primeiro de todos).
    candles_since_previous_sfp = None
    if len(marcados) >= 2:
        candles_since_previous_sfp = round((event_ts - marcados[-2]['timestamp']) / intervalo_ms, 2)

    # cluster_size_so_far: quantos membros o cluster atual já teve ATÉ
    # este evento (mesmo valor de sfp_position — é o mesmo dado, só com
    # nome mais explícito pro propósito de telemetria). NÃO é o tamanho
    # final do cluster (isso só se sabe olhando pra frente, o que
    # quebraria a causalidade) — é sempre "tamanho observado até agora".
    cluster_size_so_far = sfp_position

    return {
        'cluster_id': cluster_id,
        'cluster_direction': direcao,
        'is_first_sfp': is_first,
        'is_repeated_sfp': not is_first,
        'sfp_position': sfp_position,
        'cluster_size_so_far': cluster_size_so_far,
        'candles_since_first_sfp': candles_since_first_sfp,
        'candles_since_previous_sfp': candles_since_previous_sfp,
        'cluster_start': cluster_start,
        'cluster_last_event': event_ts,
        'total_eventos_pair_direcao': len(marcados),
    }


# ═══════════════════════════════════════════════════════════════════════
# DEDUP DE PERSISTÊNCIA — sfp_breakout_cancelado (correção pontual)
#
# Problema confirmado por evidência (audit_breakout_cancel): o mesmo
# candle/evento de breakout era reavaliado a cada ciclo do engine
# (validar_sfp_estrito não tem memória entre ciclos, de propósito) e
# CADA reavaliação incrementava contagem+1 em scalp_gates_vortex_diagnostico
# via _registrar_diagnostico_gates_vortex — 217+ ocorrências pro MESMO
# candle_event_ts em produção.
#
# Esta correção NÃO toca em validar_sfp_estrito() (a reavaliação a cada
# ciclo continua acontecendo, sem alteração nenhuma de lógica de
# detecção). Só evita que a MESMA identidade de evento incremente o
# contador mais de uma vez — reaproveitando exatamente o padrão UNIQUE +
# INSERT OR IGNORE que classify_sfp_causal() / scalp_sfp_cluster_events
# já usa. Identidade do evento = (pair, direcao, candle_event_ts).
#
# A tabela audit_breakout_cancel (auditoria bruta, sem dedup) continua
# existindo e gravando cada reavaliação, sem alteração — ela é a prova
# permanente de que o mecanismo de reavaliação existe, e serve pra
# validar esta correção depois do deploy.
# ═══════════════════════════════════════════════════════════════════════

def init_breakout_cancel_dedup_db(db_file):
    """Cria a tabela de dedup se não existir. Mesmo schema-padrão de
    scalp_sfp_cluster_events (UNIQUE(pair, direcao, timestamp))."""
    try:
        with sqlite3.connect(db_file) as conn:
            conn.execute('''
                CREATE TABLE IF NOT EXISTS scalp_breakout_cancel_events (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    pair TEXT NOT NULL,
                    direcao TEXT NOT NULL,
                    candle_event_ts INTEGER NOT NULL,
                    created_at INTEGER,
                    UNIQUE(pair, direcao, candle_event_ts)
                )
            ''')
            conn.execute('''
                CREATE INDEX IF NOT EXISTS idx_breakout_cancel_events_pair_dir
                ON scalp_breakout_cancel_events(pair, direcao)
            ''')
            conn.commit()
    except Exception as e:
        print(f"[scalp_engine breakout_cancel_dedup] erro ao criar tabela: {e}")


def _evento_breakout_cancel_e_novo(db_file, pair, direcao, candle_event_ts):
    """
    Dedup de persistência para o evento 'breakout_cancela_analise', no
    mesmo padrão de classify_sfp_causal() / scalp_sfp_cluster_events
    (UNIQUE + INSERT OR IGNORE). Identidade do evento = (pair, direcao,
    candle_event_ts) — o candle REAL que causou o breakout (capturado em
    validar_sfp_estrito(), não o timestamp do ciclo/polling.

    Retorna True se esta é a PRIMEIRA vez que este evento é visto (o
    INSERT aconteceu de fato) — o chamador deve registrar a telemetria
    normalmente. Retorna False se o evento já tinha sido registrado
    antes (reavaliação do mesmo candle num ciclo posterior) — o
    chamador deve pular o registro desta vez, sem incrementar de novo.

    Fail-open: se o DB falhar, trata como evento novo — não bloqueia a
    telemetria por causa de uma camada de proteção extra.
    """
    if candle_event_ts is None:
        # Sem candle_event_ts real não há como deduplicar com segurança;
        # deixa passar como novo (mesmo comportamento de antes do patch).
        return True

    init_breakout_cancel_dedup_db(db_file)
    try:
        with sqlite3.connect(db_file) as conn:
            cursor = conn.cursor()
            cursor.execute('''
                INSERT OR IGNORE INTO scalp_breakout_cancel_events
                    (pair, direcao, candle_event_ts, created_at)
                VALUES (?, ?, ?, ?)
            ''', (pair, direcao, candle_event_ts, int(time.time())))
            conn.commit()
            return cursor.rowcount > 0
    except Exception as e:
        print(f"[scalp_engine breakout_cancel_dedup] erro ao verificar evento {pair}/{direcao}: {e}")
        return True



# TELEMETRIA DE SFP (13/08) — camada de COLETA/DIAGNÓSTICO, conforme
# markup "Evolução do SFP Causal". Regra absoluta desta fase: estes
# campos são só instrumentação — não podem mudar sinal, ENTRY, SL, TP,
# cooldown, classify_sfp_causal() nem CLUSTER_CAUSAL. Cada linha aqui é
# 1 evento de SFP já confirmado (bloqueado ou não), com o contexto de
# cluster + HTF + premium/discount no momento em que ele foi visto —
# nada disso é reaproveitado pra decisão em process_pair_gates_vortex.
# ═══════════════════════════════════════════════════════════════════════

def _garantir_tabela_sfp_telemetria(db_file):
    """Auto-blindado como o resto do diagnóstico do gates_vortex — não
    depende de nenhuma chamada de init no boot do app.py (essa foi
    exatamente a causa dos bugs anteriores de 'no such table')."""
    try:
        with sqlite3.connect(db_file) as conn:
            conn.execute('''
                CREATE TABLE IF NOT EXISTS scalp_gates_vortex_sfp_telemetria (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    pair TEXT NOT NULL,
                    direcao TEXT NOT NULL,
                    event_ts INTEGER NOT NULL,
                    tf_label TEXT,
                    cluster_id TEXT,
                    cluster_direction TEXT,
                    sfp_position INTEGER,
                    cluster_size_so_far INTEGER,
                    is_first_sfp INTEGER,
                    is_repeated_sfp INTEGER,
                    candles_since_first_sfp REAL,
                    candles_since_previous_sfp REAL,
                    htf_bias TEXT,
                    htf_strength TEXT,
                    htf_alignment INTEGER,
                    premium_discount_state TEXT,
                    bias_context TEXT,
                    created_at INTEGER,
                    UNIQUE(pair, direcao, event_ts)
                )
            ''')
            conn.execute('''
                CREATE INDEX IF NOT EXISTS idx_sfp_telemetria_pair_dir
                ON scalp_gates_vortex_sfp_telemetria(pair, direcao)
            ''')
            conn.commit()
    except Exception as e:
        print(f"[scalp_engine sfp_telemetria] erro ao criar tabela: {e}")


def _registrar_telemetria_sfp(db_file, pair, direcao, event_ts, tf_label, cluster_info,
                               htf_context=None, premium_discount_state=None, bias_context=None):
    """
    Grava 1 linha de telemetria por evento de SFP — idempotente via
    UNIQUE(pair, direcao, event_ts) + INSERT OR IGNORE, igual ao padrão
    já usado em scalp_sfp_cluster_events (reprocessar o mesmo SFP em
    ciclos live seguintes não duplica a linha). Fail-open: qualquer erro
    aqui é só logado, nunca propaga pro pipeline principal.
    """
    _garantir_tabela_sfp_telemetria(db_file)
    htf_context = htf_context or {}
    try:
        with sqlite3.connect(db_file) as conn:
            conn.execute('''
                INSERT OR IGNORE INTO scalp_gates_vortex_sfp_telemetria
                    (pair, direcao, event_ts, tf_label, cluster_id, cluster_direction,
                     sfp_position, cluster_size_so_far, is_first_sfp, is_repeated_sfp,
                     candles_since_first_sfp, candles_since_previous_sfp,
                     htf_bias, htf_strength, htf_alignment, premium_discount_state,
                     bias_context, created_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ''', (
                pair, direcao, event_ts, tf_label,
                cluster_info.get('cluster_id'), cluster_info.get('cluster_direction'),
                cluster_info.get('sfp_position'), cluster_info.get('cluster_size_so_far'),
                1 if cluster_info.get('is_first_sfp') else 0,
                1 if cluster_info.get('is_repeated_sfp') else 0,
                cluster_info.get('candles_since_first_sfp'), cluster_info.get('candles_since_previous_sfp'),
                htf_context.get('bias'), htf_context.get('strength'),
                1 if htf_context.get('alignment', {}).get('aligned') else 0 if htf_context.get('alignment') else None,
                premium_discount_state, bias_context, int(time.time()),
            ))
            conn.commit()
    except Exception as e:
        print(f"[scalp_engine sfp_telemetria] erro ao registrar {pair}/{direcao}: {e}")


def sfp_telemetria_report(db_file, pair=None, limit=200):
    """
    Consulta a telemetria bruta, com bucket de sfp_position e
    cluster_size_so_far já agrupados (seções 5-6 do markup), pra dar
    exemplos reais e permitir cruzar por WIN/LOSS depois — sem calcular
    nenhum score, só devolvendo os dados crus + contagens.
    """
    _garantir_tabela_sfp_telemetria(db_file)
    try:
        with sqlite3.connect(db_file) as conn:
            cursor = conn.cursor()
            cols = (
                "pair, direcao, event_ts, tf_label, cluster_id, cluster_direction, "
                "sfp_position, cluster_size_so_far, is_first_sfp, is_repeated_sfp, "
                "candles_since_first_sfp, candles_since_previous_sfp, "
                "htf_bias, htf_strength, htf_alignment, premium_discount_state, "
                "bias_context, created_at"
            )
            if pair:
                cursor.execute(
                    f"SELECT {cols} FROM scalp_gates_vortex_sfp_telemetria WHERE pair=? "
                    f"ORDER BY event_ts DESC LIMIT ?",
                    (pair, limit)
                )
            else:
                cursor.execute(
                    f"SELECT {cols} FROM scalp_gates_vortex_sfp_telemetria "
                    f"ORDER BY event_ts DESC LIMIT ?",
                    (limit,)
                )
            nomes_col = [c.strip() for c in cols.split(',')]
            rows = [dict(zip(nomes_col, row)) for row in cursor.fetchall()]
    except Exception as e:
        return {'erro': str(e)}

    def bucket_position(pos):
        if pos is None:
            return 'desconhecido'
        if pos == 1:
            return '1_primeiro'
        if pos in (2,):
            return '2'
        if pos == 3:
            return '3'
        if 4 <= pos <= 5:
            return '4-5'
        if 6 <= pos <= 10:
            return '6-10'
        return '10+'

    def bucket_size(n):
        if n is None:
            return 'desconhecido'
        if n == 1:
            return '1'
        if 2 <= n <= 3:
            return '2-3'
        if 4 <= n <= 5:
            return '4-5'
        if 6 <= n <= 10:
            return '6-10'
        if 11 <= n <= 20:
            return '11-20'
        return '21+'

    contagem_por_posicao = {}
    contagem_por_tamanho = {}
    for r in rows:
        bp = bucket_position(r.get('sfp_position'))
        bs = bucket_size(r.get('cluster_size_so_far'))
        contagem_por_posicao[bp] = contagem_por_posicao.get(bp, 0) + 1
        contagem_por_tamanho[bs] = contagem_por_tamanho.get(bs, 0) + 1

    return {
        'total_registros': len(rows),
        'contagem_por_bucket_posicao': contagem_por_posicao,
        'contagem_por_bucket_tamanho_ate_agora': contagem_por_tamanho,
        'registros': rows,
    }


def _detectar_sobreposicao_sfp(eventos_sfp, horizonte_candles=20, intervalo_ms=900000):
    """
    Só MEDE — não remove nem resolve nada. Verifica se existem vários SFPs
    antes de terminar o horizonte de `horizonte_candles` de um SFP
    anterior (mesma referência). Ordenado por timestamp, busca early-exit
    (para assim que o próximo evento já passa da janela).
    """
    if not eventos_sfp:
        return {'total_sfp': 0, 'eventos_sobrepostos': 0, 'taxa_sobreposicao_pct': None}

    eventos_ordenados = sorted(eventos_sfp, key=lambda e: e['timestamp'])
    janela_ms = horizonte_candles * intervalo_ms
    n = len(eventos_ordenados)
    sobrepostos = 0

    for i in range(n):
        limite = eventos_ordenados[i]['timestamp'] + janela_ms
        for j in range(i + 1, n):
            if eventos_ordenados[j]['timestamp'] > limite:
                break
            sobrepostos += 1
            break  # só precisa saber que existe PELO MENOS 1 sobreposição

    return {
        'total_sfp': n,
        'eventos_sobrepostos': sobrepostos,
        'taxa_sobreposicao_pct': round(100 * sobrepostos / n, 1),
    }


def _agregar_qualidade_fase2(eventos_sfp, horizonte_referencia=20):
    """
    Transforma a lista de eventos SFP individuais (Fase 2) na tabela
    resumida: MFE/MAE médio+mediano+percentis, taxa de atingir cada alvo
    favorável/adverso (medido no horizonte de referência, default 20
    candles), e expectancy real por cenário de TP/SL fixo — não usa
    rr_proxy_medio_das_razoes (marcado NAO_VALIDADA), calcula expectancy
    direto dos resultados TP/SL/AMBIGUO/NENHUM.
    """
    if not eventos_sfp:
        return {'total_sfp': 0}

    mfes = sorted(e['horizontes'][horizonte_referencia]['mfe_pct'] for e in eventos_sfp if e['horizontes'].get(horizonte_referencia))
    maes = sorted(e['horizontes'][horizonte_referencia]['mae_pct'] for e in eventos_sfp if e['horizontes'].get(horizonte_referencia))

    def media(lista):
        return round(sum(lista) / len(lista), 4) if lista else None

    def mediana(lista_ordenada):
        return _percentil(lista_ordenada, 50)

    taxas_favoravel = {}
    for alvo in NIVEIS_ALVO_FAVORAVEL_PCT:
        atingiram = sum(1 for m in mfes if m >= alvo)
        taxas_favoravel[f'{alvo}%'] = round(100 * atingiram / len(mfes), 1) if mfes else None

    taxas_adverso = {}
    for alvo in NIVEIS_ALVO_ADVERSO_PCT:
        sofreram = sum(1 for m in maes if m >= alvo)
        taxas_adverso[f'{alvo}%'] = round(100 * sofreram / len(maes), 1) if maes else None

    cenarios_agregados = {}
    for tp_pct, sl_pct in CENARIOS_RR_FIXOS:
        chave = f'{tp_pct}/{sl_pct}'
        outcomes = [e['cenarios_rr'][chave] for e in eventos_sfp if chave in e.get('cenarios_rr', {})]
        n_win = outcomes.count('TP')   # TP bateu primeiro = WIN
        n_loss = outcomes.count('SL')  # SL bateu primeiro = LOSS
        n_ambiguo = outcomes.count('AMBIGUO')  # TP e SL no MESMO candle — fica separado, não conta no win/loss
        n_nenhum = outcomes.count('NENHUM')
        n_resolvidos = n_win + n_loss  # AMBIGUO e NENHUM ficam FORA da estatística principal
        win_rate = round(n_win / n_resolvidos, 3) if n_resolvidos else None
        loss_rate = round(1 - win_rate, 3) if win_rate is not None else None
        expectancy_pct = round((win_rate * tp_pct) - (loss_rate * sl_pct), 4) if win_rate is not None else None
        cenarios_agregados[chave] = {
            'WIN': n_win, 'LOSS': n_loss, 'AMBIGUO': n_ambiguo, 'NENHUM': n_nenhum,
            'win_rate': win_rate, 'loss_rate': loss_rate, 'expectancy_pct': expectancy_pct,
        }

    return {
        'total_sfp': len(eventos_sfp),
        'horizonte_referencia_candles': horizonte_referencia,
        'mfe_medio_pct': media(mfes), 'mfe_mediano_pct': mediana(mfes),
        'mfe_p25_pct': _percentil(mfes, 25), 'mfe_p50_pct': _percentil(mfes, 50), 'mfe_p75_pct': _percentil(mfes, 75),
        'mae_medio_pct': media(maes), 'mae_mediano_pct': mediana(maes),
        'mae_p25_pct': _percentil(maes, 25), 'mae_p50_pct': _percentil(maes, 50), 'mae_p75_pct': _percentil(maes, 75),
        'taxa_atingir_alvo_favoravel': taxas_favoravel,
        'taxa_sofrer_alvo_adverso': taxas_adverso,
        'cenarios_rr_fixos': cenarios_agregados,
    }


def replay_comparar_referencias_liquidez(pair, dias_historico=30, forward_lookahead=20):
    """
    Orquestra o replay: pra cada dia do histórico, pra cada uma das 4
    referências de liquidez, calcula bias/direção (reaproveitando as
    MESMAS funções de produção, sem reimplementar), classifica o
    resultado (TOUCH/SWEEP/RECLAIM/SFP/BREAKOUT/NUNCA_TOCOU), e mede
    qualidade dos SFPs encontrados (MFE/MAE nos `forward_lookahead`
    candles M15 seguintes, como proxy de RR potencial e continuidade).
    """
    symbol_map = {
        'BTCUSD': 'BTCUSDT', 'ETHUSD': 'ETHUSDT', 'SOLUSD': 'SOLUSDT', 'XRPUSD': 'XRPUSDT',
        'LINKUSD': 'LINKUSDT', 'ADAUSD': 'ADAUSDT', 'AVAXUSD': 'AVAXUSDT', 'BNBUSD': 'BNBUSDT',
        'AAVEUSD': 'AAVEUSDT', 'NEARUSD': 'NEARUSDT', 'PENDLEUSD': 'PENDLEUSDT', 'INJUSD': 'INJUSDT',
    }
    symbol = symbol_map.get(pair.upper(), pair.upper().replace('USD', 'USDT'))

    d1_bruto = _fetch_bybit_klines_historico(symbol, 'D', dias_historico + 20)
    m15_bruto = _fetch_bybit_klines_historico(symbol, '15', dias_historico + 3)

    d1, validacao_d1 = _validar_e_limpar_candles(d1_bruto, 'D')
    m15, validacao_m15 = _validar_e_limpar_candles(m15_bruto, '15')

    if len(d1) < 15 or len(m15) < 100:
        return {
            'erro': f'dados históricos insuficientes pra {pair} (D1={len(d1)}, M15={len(m15)}) — mesmo após limpeza',
            'validacao_d1': validacao_d1, 'validacao_m15': validacao_m15,
        }

    resultado_por_ref = {ref: {'amostras': [], 'sfps_qualidade': []} for ref in REFERENCIAS_LIQUIDEZ}

    dias_unicos = sorted(set(datetime.fromtimestamp(c['t'] / 1000, tz=timezone.utc).date() for c in d1))
    inicio_analise = max(15, len(dias_unicos) - dias_historico)

    for i in range(inicio_analise, len(dias_unicos)):
        dia_atual = dias_unicos[i]
        d1_ate_ontem = [c for c in d1 if datetime.fromtimestamp(c['t'] / 1000, tz=timezone.utc).date() < dia_atual]
        if len(d1_ate_ontem) < 8:
            continue

        m15_do_dia = [c for c in m15 if datetime.fromtimestamp(c['t'] / 1000, tz=timezone.utc).date() == dia_atual]
        if not m15_do_dia:
            continue

        midnight_open = _find_open_at_hour(m15_do_dia, 0, timezone.utc)
        if midnight_open is None:
            continue

        for ref_nome, ref_fn in REFERENCIAS_LIQUIDEZ.items():
            liquidez = ref_fn(d1_ate_ontem)
            if not liquidez:
                continue

            for idx, c in enumerate(m15_do_dia):
                direcao_permitida = 'baixa' if c['c'] > midnight_open else 'alta'
                resto_do_dia = m15_do_dia[idx:]
                caso, candle_evento = _classificar_periodo_liquidez(resto_do_dia, liquidez['high'], liquidez['low'], direcao_permitida)

                distancia_pct = None
                nivel_relevante = liquidez['high'] if direcao_permitida == 'baixa' else liquidez['low']
                if nivel_relevante and c['c']:
                    distancia_pct = abs(nivel_relevante - c['c']) / c['c'] * 100

                resultado_por_ref[ref_nome]['amostras'].append({
                    'dia': str(dia_atual), 'direcao': direcao_permitida,
                    'caso': caso, 'distancia_pct': round(distancia_pct, 3) if distancia_pct else None,
                })

                if caso == 'SFP' and candle_evento:
                    idx_evento = m15.index(candle_evento) if candle_evento in m15 else None
                    if idx_evento is not None:
                        janela_forward = m15[idx_evento + 1: idx_evento + 1 + forward_lookahead]
                        entry = candle_evento['c']
                        if janela_forward:
                            if direcao_permitida == 'baixa':
                                mfe = max(0, entry - min(c['l'] for c in janela_forward))
                                mae = max(0, max(c['h'] for c in janela_forward) - entry)
                            else:
                                mfe = max(0, max(c['h'] for c in janela_forward) - entry)
                                mae = max(0, entry - min(c['l'] for c in janela_forward))
                            resultado_por_ref[ref_nome]['sfps_qualidade'].append({
                                'dia': str(dia_atual), 'mfe_pct': round(mfe / entry * 100, 3),
                                'mae_pct': round(mae / entry * 100, 3),
                                'rr_proxy': round(mfe / mae, 2) if mae > 0 else None,
                            })

                        nivel_liq = liquidez['high'] if direcao_permitida == 'baixa' else liquidez['low']
                        distancia_pre_sweep_pct = round(abs(nivel_liq - entry) / entry * 100, 4)
                        qualidade_detalhada = _avaliar_qualidade_sfp_evento(m15, idx_evento, direcao_permitida, entry)
                        movimento_intra_candle = _medir_movimento_durante_candle(candle_evento, direcao_permitida, entry)
                        if qualidade_detalhada:
                            resultado_por_ref[ref_nome].setdefault('eventos_sfp', []).append({
                                'pair': pair, 'timestamp': candle_evento['t'], 'direcao': direcao_permitida,
                                'referencia': ref_nome, 'nivel_liquidez': round(nivel_liq, 6),
                                'entry': round(entry, 6), 'high': candle_evento['h'], 'low': candle_evento['l'],
                                'close': candle_evento['c'], 'distancia_pre_sweep_pct': distancia_pre_sweep_pct,
                                **movimento_intra_candle,
                                **qualidade_detalhada,
                            })
                # SEM break — cada candle M15 do dia é seu próprio ponto de
                # observação (candle a candle, não só a abertura do dia).

    relatorio = {}
    for ref_nome, dados in resultado_por_ref.items():
        amostras = dados['amostras']
        total = len(amostras)
        if total == 0:
            relatorio[ref_nome] = {'amostras': 0}
            continue
        contagem = {}
        for a in amostras:
            contagem[a['caso']] = contagem.get(a['caso'], 0) + 1
        distancias_validas = [a['distancia_pct'] for a in amostras if a['distancia_pct'] is not None]
        dist_media = round(sum(distancias_validas) / len(distancias_validas), 3) if distancias_validas else None
        dist_ordenadas = sorted(distancias_validas)
        dist_mediana = round(dist_ordenadas[len(dist_ordenadas) // 2], 3) if dist_ordenadas else None
        dist_maxima = round(max(dist_ordenadas), 3) if dist_ordenadas else None

        n_sfp = contagem.get('SFP', 0)
        n_breakout = contagem.get('BREAKOUT', 0)
        n_touch_sem_sweep = contagem.get('TOUCH_SEM_SWEEP', 0)
        n_nunca_tocou = contagem.get('NUNCA_TOCOU', 0)
        n_sweep = n_sfp + n_breakout  # SWEEP = todo caso que ultrapassou (SFP ou BREAKOUT)
        n_touch = n_sweep + n_touch_sem_sweep  # TOUCH = qualquer contato, com ou sem sweep
        n_reclaim = n_sfp  # RECLAIM só é contabilizado quando junto forma SFP (ver nota abaixo)

        qualidade = dados['sfps_qualidade']
        rr_validos = [q['rr_proxy'] for q in qualidade if q['rr_proxy'] is not None]
        # rr_proxy_medio = média das RAZÕES individuais (mfe/mae por caso) —
        # estatisticamente frágil: 1 caso com mae≈0 já domina a média inteira.
        # Mantido só por transparência, mas marcado NAO_VALIDADA — não usar
        # pra concluir nada até substituir por métrica mais robusta.
        rr_medio_fragil = round(sum(rr_validos) / len(rr_validos), 2) if rr_validos else None

        mfe_medio_calc = round(sum(q['mfe_pct'] for q in qualidade) / len(qualidade), 3) if qualidade else None
        mae_medio_calc = round(sum(q['mae_pct'] for q in qualidade) / len(qualidade), 3) if qualidade else None
        # Razão das médias (mfe_médio / mae_médio) — muito mais robusta a outliers
        rr_razao_das_medias = round(mfe_medio_calc / mae_medio_calc, 2) if (mfe_medio_calc and mae_medio_calc and mae_medio_calc > 0) else None
        # Mediana das razões individuais — também robusta, não deixa 1 outlier dominar
        rr_mediana = None
        if rr_validos:
            rr_ordenados = sorted(rr_validos)
            rr_mediana = round(rr_ordenados[len(rr_ordenados) // 2], 2)

        relatorio[ref_nome] = {
            'amostras': total,
            'contagem_absoluta': {
                'TOUCH': n_touch, 'SWEEP': n_sweep, 'RECLAIM': n_reclaim,
                'SFP': n_sfp, 'BREAKOUT': n_breakout, 'NUNCA_TOCOU': n_nunca_tocou,
            },
            'taxas': {
                'SFP_RATE': round(n_sfp / n_sweep, 3) if n_sweep else None,
                'RECLAIM_RATE': round(n_reclaim / n_sweep, 3) if n_sweep else None,
                'BREAKOUT_RATE': round(n_breakout / n_sweep, 3) if n_sweep else None,
                'TOUCH_TO_SWEEP': round(n_sweep / n_touch, 3) if n_touch else None,
            },
            'distribuicao_pct': {k: round(100 * v / total, 1) for k, v in contagem.items()},
            'distancia_media_pct': dist_media,
            'distancia_mediana_pct': dist_mediana,
            'distancia_maxima_pct': dist_maxima,
            'sfps_encontrados': len(qualidade),
            'rr_proxy_medio_das_razoes': {'valor': rr_medio_fragil, 'status': 'NAO_VALIDADA', 'motivo': 'média de razões individuais mfe/mae — 1 caso com mae baixo já domina o resultado, não usar pra concluir nada'},
            'rr_razao_das_medias': rr_razao_das_medias,
            'rr_mediana_das_razoes': rr_mediana,
            'mfe_medio_pct': mfe_medio_calc,
            'mae_medio_pct': mae_medio_calc,
            'qualidade_fase2': _agregar_qualidade_fase2(dados.get('eventos_sfp', [])),
            'sobreposicao_sfp': _detectar_sobreposicao_sfp(dados.get('eventos_sfp', [])),
            'clusters_sfp': _agrupar_clusters_sfp(dados.get('eventos_sfp', [])),
            'movimento_intra_candle_vs_pos_entry': _comparar_movimento_intra_vs_pos_entry(dados.get('eventos_sfp', [])),
            'qualidade_por_posicao_no_cluster': _qualidade_primeiro_vs_repetido(dados.get('eventos_sfp', [])),
            'qualidade_por_direcao': _qualidade_por_direcao(dados.get('eventos_sfp', [])),
        }

    return {
        'pair': pair, 'dias_historico': dias_historico,
        'validacao_dados': {'D1': validacao_d1, 'M15': validacao_m15},
        'notas': {
            'SWING_D1': 'NÃO é máximo/mínimo local nem range extremo. É uma referência '
                        'estrutural baseada em mudança de leg (_extrair_swings_lux_algo) — '
                        'só registra um nível quando há reversão genuína de tendência, não '
                        'quando o preço só toca um extremo. Se gerar poucas oportunidades, '
                        'isso significa que a definição é mais seletiva, não que está errada.',
            'RECLAIM_SFP': 'No modelo atual, RECLAIM e SFP são o MESMO evento, não duas '
                            'amostras independentes — o reclaim é confirmado no próprio '
                            'candle do sweep (sweep + fechamento de volta no mesmo candle '
                            'fechado = SFP). Os dois números do relatório sempre baterão.',
            'TOUCH_SWEEP': 'TOUCH (>=) e SWEEP (>) batem praticamente sempre com dado real '
                            '— não é bug de contador nem estados sobrescritos. Com preço '
                            'contínuo (float), a chance de high/low bater EXATAMENTE igual '
                            'ao nível de liquidez é matematicamente próxima de zero. '
                            'Confirmado com teste isolado (ver auditoria). TOUCH_TO_SWEEP '
                            'sempre vai ficar perto de 1.0 nesse formato de dado.',
            'RR_PROXY': 'rr_proxy_medio_das_razoes usa média das razões individuais '
                        '(mfe/mae por caso) — estatisticamente frágil, 1 caso com mae baixo '
                        'já domina o resultado. Marcado NAO_VALIDADA. Usar '
                        'rr_razao_das_medias ou rr_mediana_das_razoes, mais robustos.',
            'ENTRY': 'ENTRY = close (fechamento) do candle onde o SFP foi confirmado '
                     '(sweep + reclaim no mesmo candle). NÃO é abertura do candle seguinte '
                     '— uma única metodologia, sem mistura.',
            'AMBIGUO_TP_SL': 'Se TP e SL forem tocados no MESMO candle (high>=TP e low<=SL '
                              'juntos), o resultado fica AMBIGUO — não dá pra saber qual '
                              'bateu primeiro sem dado intrabar. AMBIGUO e NENHUM ficam '
                              'FORA do win_rate/loss_rate/expectancy (só WIN+LOSS contam).',
            'SOBREPOSICAO_SFP': 'sobreposicao_sfp só MEDE se existe outro SFP dentro dos '
                                 '20 candles seguintes de um SFP anterior (mesma referência) '
                                 '— não remove nem resolve nada automaticamente.',
        },
        'por_referencia': relatorio,
    }


# ═══════════════════════════════════════════════════════════════════════
# SHADOW/REPLAY — gates_reprovados (Fase 1)
#
# Investigação de telemetria pura: "as oportunidades que o Kairos
# rejeitou por falha em algum gate A-G teriam dado TP ou SL?"
#
# NÃO altera nenhuma função de produção. NÃO é chamado por nenhum ciclo
# automático. Reaproveita process_pair_gates_vortex() de verdade (mesma
# lógica de bias/SFP/MSS/FVG/gates), mas SEMPRE com um db_file temporário
# e descartável — nunca com o banco de produção. Os resultados de cada
# oportunidade (entry/sl/tp1/tp2/direção/gates) são lidos do dict de
# retorno, não de nenhuma tabela.
#
# Ambiguidade resolvida (documentada, não inventada): não existe
# histórico persistido de eventos gates_reprovados com entry/sl/tp
# (scalp_gates_vortex_diagnostico só guarda contador agregado). Por
# isso o replay recalcula retroativamente, candle a candle, usando
# candles históricos reais da Bybit — mesmo padrão de
# replay_comparar_referencias_liquidez().
# ═══════════════════════════════════════════════════════════════════════

import tempfile
import os


GATES_REPROVADOS_HORIZONTES_CANDLES = [20, 50, 100]
GATES_REPROVADOS_EXEC_TF_MS = 300000  # M5 — mesmo timeframe de gatilho da produção
GATES_REPROVADOS_EXEC_TF_SEG = 300    # mesmo valor em segundos, pra alimentar agora_ts em dados_obsoletos()


def _montar_candles_por_tf_ate(d1, h4, h1, m15, m5, m1, ts_corte_ms):
    """
    Monta o dict candles_por_tf exatamente no formato que
    process_pair_gates_vortex() espera, mas só com candles cujo
    timestamp de ABERTURA é <= ts_corte_ms — ou seja, só o que já
    tinha fechado (ou estava fechando) naquele ponto do histórico.
    Isso é o que garante ausência de lookahead: nenhuma função de
    produção enxerga candle futuro.
    """
    def corta(candles):
        if not candles:
            return candles
        return [c for c in candles if c['t'] <= ts_corte_ms]

    return {
        'D1': corta(d1), 'H4': corta(h4), 'H1': corta(h1),
        'M15': corta(m15), 'M5': corta(m5), 'M1': corta(m1),
    }


CENARIOS_SIMULACAO_GATES = [
    'atual', 'sem_F_ICHIMOKU_INFO', 'score_6_de_7', 'score_5_de_7',
    'sem_A_MTF_ALIGNMENT', 'sem_B_TRIGGER', 'sem_C_MONTE_CARLO',
]


def _passa_cenario_simulado(gates_dict, cenario):
    """
    SIMULAÇÃO PURA — combina os booleanos JÁ DECIDIDOS pela produção
    (gates_dict = resultado['gates'], vindo direto de
    process_pair_gates_vortex(), sem alteração). NENHUM gate é
    recalculado aqui — só recombino os 7 resultados finais de formas
    alternativas, pra medir "e se a regra de combinação fosse outra".
    Não decide nada em produção, é só análise de replay.
    """
    if cenario == 'atual':
        return all(gates_dict.values())
    if cenario == 'sem_F_ICHIMOKU_INFO':
        return all(v for k, v in gates_dict.items() if k != 'F_ICHIMOKU_INFO')
    if cenario == 'score_6_de_7':
        return sum(1 for v in gates_dict.values() if v) >= 6
    if cenario == 'score_5_de_7':
        return sum(1 for v in gates_dict.values() if v) >= 5
    if cenario.startswith('sem_'):
        gate_removido = cenario[len('sem_'):]
        return all(v for k, v in gates_dict.items() if k != gate_removido)
    return False


def _simular_cenarios_gates(avaliacoes_gates_reprovados_brutas, m5, pair):
    """
    Roda os CENARIOS_SIMULACAO_GATES sobre o mesmo conjunto de
    avaliações já capturado (nenhuma nova chamada a
    process_pair_gates_vortex(), nenhum gate recalculado). Pra cada
    cenário: filtra quais avaliações BRUTAS "passariam" sob aquela
    regra alternativa, reconstrói identidade/clusters (mesmo mecanismo
    já aprovado, sem janela arbitrária), pega 1 representante por
    cluster, e resolve TP/SL/AMBIGUO/NENHUM nos candles futuros REAIS
    (sem lookahead — só candles com t > ts_corte do representante).
    """
    resultado_cenarios = {}
    for cenario in CENARIOS_SIMULACAO_GATES:
        avals_que_passam = [
            av for av in avaliacoes_gates_reprovados_brutas
            if _passa_cenario_simulado(av['gates'], cenario)
        ]
        _, clusters = _reconstruir_identidade_temporal_gates(avals_que_passam)

        candidatos = []
        for c in clusters:
            if not c['identidade_valida']:
                continue
            primeira_av = next(
                (av for av in avals_que_passam
                 if av['ts_corte'] == c['primeiro_ts'] and av['direcao'] == c['direcao']
                 and av['entry'] == c['entry'] and av['sl'] == c['sl']),
                None,
            )
            if primeira_av is None:
                continue
            candidatos.append({
                'direcao': c['direcao'], 'entry': c['entry'], 'sl': c['sl'],
                'tp1': c['tp1'], 'tp2': c['tp2'], 'ts_corte': c['primeiro_ts'],
                'idx_m5': primeira_av['idx_m5'],
                'gates_reprovados_lista_original': primeira_av['gates_reprovados_lista'],
                'cluster_tamanho_bruto': c['tamanho'],
            })

        # ── Resolver TP/SL sem lookahead, horizonte único de 20 candles
        # M5 (suficiente pra medir WIN/LOSS/AMBIGUO/NENHUM; os outros
        # horizontes já aprovados continuam disponíveis se precisar
        # ampliar depois). ──
        resolvidos = []
        for cand in candidatos:
            candles_futuros = m5[cand['idx_m5'] + 1:]
            res = _resolver_tp_sl_futuro(
                candles_futuros, cand['direcao'], cand['entry'], cand['sl'],
                cand['tp1'], cand['tp2'], max_candles=20,
            )
            resolvidos.append({**cand, **res})

        n = len(resolvidos)
        n_tp1 = sum(1 for r in resolvidos if r['resultado'] == 'TP1')
        n_tp2 = sum(1 for r in resolvidos if r['resultado'] == 'TP2')
        n_sl = sum(1 for r in resolvidos if r['resultado'] == 'SL')
        n_amb = sum(1 for r in resolvidos if r['resultado'] == 'AMBIGUO')
        n_nen = sum(1 for r in resolvidos if r['resultado'] == 'NENHUM')
        n_win = n_tp1 + n_tp2
        n_resolvidos_binario = n_win + n_sl
        win_rate = round(100 * n_win / n_resolvidos_binario, 1) if n_resolvidos_binario else None

        mfes = [r['mfe_pct'] for r in resolvidos]
        maes = [r['mae_pct'] for r in resolvidos]
        mfe_medio = round(sum(mfes) / len(mfes), 4) if mfes else None
        mae_medio = round(sum(maes) / len(maes), 4) if maes else None

        rrs = []
        for r in resolvidos:
            if r['resultado'] in ('TP1', 'SL') and r['entry'] and r['sl'] and r['tp1']:
                risco = abs(r['entry'] - r['sl'])
                retorno = abs(r['tp1'] - r['entry'])
                if risco > 0:
                    rrs.append(retorno / risco)
        rr_medio = round(sum(rrs) / len(rrs), 2) if rrs else None
        expectancy = None
        if win_rate is not None and rr_medio is not None:
            wr = win_rate / 100
            expectancy = round((wr * rr_medio) - (1 - wr), 4)

        resultado_cenarios[cenario] = {
            'entradas_simuladas': n,
            'avaliacoes_brutas_que_passariam': len(avals_que_passam),
            'TP1': n_tp1, 'TP2': n_tp2, 'SL': n_sl, 'AMBIGUO': n_amb, 'NENHUM': n_nen,
            'win_rate_pct': win_rate,
            'mfe_medio_pct': mfe_medio, 'mae_medio_pct': mae_medio,
            'rr_medio_realizado': rr_medio,
            'expectancy': expectancy,
            'expectancy_nota': (
                None if rr_medio is not None else
                'RR/expectancy não validável — amostra sem casos TP1/SL suficientes'
            ),
            'detalhe_candidatos': resolvidos,
        }
    return resultado_cenarios



def _reconstruir_identidade_temporal_gates(avaliacoes_ordenadas):
    """
    Reconstrói a identidade temporal das avaliações de gates_reprovados
    SEM inventar dedup por janela arbitrária. Cada avaliação já vem com
    entry/sl/tp1/tp2/direcao REAIS (capturados via debug_gates=True,
    direto dos valores locais de process_pair_gates_vortex(), não do
    dict público que fica None nesse branch).

    Calcula, pra cada avaliação: distância em segundos até a anterior,
    se mudou direção/entry/SL em relação à anterior.

    Depois agrupa em clusters SOMENTE quando avaliações CONSECUTIVAS no
    tempo têm entry+sl+direção IDÊNTICOS — isso não é uma janela de N
    candles, é uma checagem de igualdade real entre vizinhos diretos.
    Se a identidade mudar (mesmo que só um pouco), o cluster fecha ali
    e um novo começa. Isso é o mesmo princípio causal de
    classify_sfp_causal(), mas comparando IDENTIDADE, não só tempo.

    Retorna (avaliacoes_com_delta, clusters).
    """
    avaliacoes_com_delta = []
    anterior = None
    for av in avaliacoes_ordenadas:
        delta_seg = None
        mudou_direcao = mudou_entry = mudou_sl = None
        if anterior is not None:
            delta_seg = round((av['ts_corte'] - anterior['ts_corte']) / 1000, 1)
            mudou_direcao = av['direcao'] != anterior['direcao']
            mudou_entry = av['entry'] != anterior['entry']
            mudou_sl = av['sl'] != anterior['sl']
        avaliacoes_com_delta.append({
            **av,
            'delta_segundos_desde_anterior': delta_seg,
            'mudou_direcao_vs_anterior': mudou_direcao,
            'mudou_entry_vs_anterior': mudou_entry,
            'mudou_sl_vs_anterior': mudou_sl,
        })
        anterior = av

    clusters = []
    cluster_atual = None
    for av in avaliacoes_com_delta:
        identidade = (av['direcao'], av['entry'], av['sl'])
        identidade_valida = av['entry'] is not None and av['sl'] is not None and av['direcao'] is not None
        if cluster_atual is not None and cluster_atual['identidade'] == identidade and identidade_valida:
            cluster_atual['tamanho'] += 1
            cluster_atual['ultimo_ts'] = av['ts_corte']
            cluster_atual['gates_reprovados_por_avaliacao'].append(av['gates_reprovados_lista'])
        else:
            if cluster_atual is not None:
                clusters.append(cluster_atual)
            cluster_atual = {
                'identidade': identidade,
                'identidade_valida': identidade_valida,
                'direcao': av['direcao'], 'entry': av['entry'], 'sl': av['sl'],
                'tp1': av['tp1'], 'tp2': av['tp2'],
                'primeiro_ts': av['ts_corte'], 'ultimo_ts': av['ts_corte'],
                'tamanho': 1,
                'gates_reprovados_por_avaliacao': [av['gates_reprovados_lista']],
            }
    if cluster_atual is not None:
        clusters.append(cluster_atual)

    for c in clusters:
        del c['identidade']  # tupla auxiliar, não serializa bem em JSON
        c['duracao_segundos'] = round((c['ultimo_ts'] - c['primeiro_ts']) / 1000, 1)

    return avaliacoes_com_delta, clusters


def _resolver_tp_sl_futuro(candles_gatilho_futuros, direcao, entry, sl, tp1, tp2, max_candles):
    """
    Réplica do padrão já usado em _avaliar_qualidade_sfp_evento(): anda
    candle a candle nos candles FUTUROS reais (nunca usa preço do
    momento em que o replay roda), verifica qual nível é atingido
    primeiro. Sem lookahead: só olha pra frente do candle de entrada,
    nunca usa resultado futuro pra decidir se o setup existia.

    Se TP e SL forem tocados no MESMO candle, marca AMBIGUO (não
    inventa ordem intrabar). Se nada for tocado dentro de max_candles,
    marca NENHUM.
    """
    janela = candles_gatilho_futuros[:max_candles]
    if not janela:
        return {'resultado': 'NENHUM', 'candles_ate_resolucao': None, 'mfe_pct': 0.0, 'mae_pct': 0.0}

    mfe = 0.0
    mae = 0.0

    for idx, c in enumerate(janela):
        if direcao == 'alta':
            avanco = c['h'] - entry
            recuo = entry - c['l']
        else:
            avanco = entry - c['l']
            recuo = c['h'] - entry
        mfe = max(mfe, avanco)
        mae = max(mae, recuo)

        if direcao == 'alta':
            bateu_sl = c['l'] <= sl
            bateu_tp1 = tp1 is not None and c['h'] >= tp1
            bateu_tp2 = tp2 is not None and c['h'] >= tp2
        else:
            bateu_sl = c['h'] >= sl
            bateu_tp1 = tp1 is not None and c['l'] <= tp1
            bateu_tp2 = tp2 is not None and c['l'] <= tp2

        if bateu_sl and (bateu_tp1 or bateu_tp2):
            return {
                'resultado': 'AMBIGUO', 'candles_ate_resolucao': idx + 1,
                'mfe_pct': round(mfe / entry * 100, 4), 'mae_pct': round(mae / entry * 100, 4),
            }
        if bateu_tp2:
            return {
                'resultado': 'TP2', 'candles_ate_resolucao': idx + 1,
                'mfe_pct': round(mfe / entry * 100, 4), 'mae_pct': round(mae / entry * 100, 4),
            }
        if bateu_tp1:
            return {
                'resultado': 'TP1', 'candles_ate_resolucao': idx + 1,
                'mfe_pct': round(mfe / entry * 100, 4), 'mae_pct': round(mae / entry * 100, 4),
            }
        if bateu_sl:
            return {
                'resultado': 'SL', 'candles_ate_resolucao': idx + 1,
                'mfe_pct': round(mfe / entry * 100, 4), 'mae_pct': round(mae / entry * 100, 4),
            }

    return {
        'resultado': 'NENHUM', 'candles_ate_resolucao': None,
        'mfe_pct': round(mfe / entry * 100, 4), 'mae_pct': round(mae / entry * 100, 4),
    }


def replay_gates_reprovados(pair, dias_historico=30):
    """
    Shadow/replay da Fase 1: varre o histórico real do par, chamando
    process_pair_gates_vortex() (função de PRODUÇÃO, sem alteração)
    contra um db_file TEMPORÁRIO e descartável a cada M5 fechado, e
    coleta toda ocorrência cuja resultado['motivo'] contenha 'falhou
    nos gates' (== categoria gates_reprovados). Cada oportunidade única
    (dedup por pair+direção+entry+sl) é levada adiante nos horizontes
    de 20/50/100 candles M5, usando só candles futuros reais.

    NÃO escreve em nenhuma tabela de produção. NÃO é chamado por nenhum
    ciclo automático. NÃO altera process_pair_gates_vortex() nem
    qualquer outra função de produção.
    """
    symbol_map = {
        'BTCUSD': 'BTCUSDT', 'ETHUSD': 'ETHUSDT', 'SOLUSD': 'SOLUSDT', 'XRPUSD': 'XRPUSDT',
        'LINKUSD': 'LINKUSDT', 'ADAUSD': 'ADAUSDT', 'AVAXUSD': 'AVAXUSDT', 'BNBUSD': 'BNBUSDT',
        'AAVEUSD': 'AAVEUSDT', 'NEARUSD': 'NEARUSDT', 'PENDLEUSD': 'PENDLEUSDT', 'INJUSD': 'INJUSDT',
        'ONDOUSD': 'ONDOUSDT',
    }
    symbol = symbol_map.get(pair.upper(), pair.upper().replace('USD', 'USDT'))

    d1_bruto = _fetch_bybit_klines_historico(symbol, 'D', dias_historico + 20)
    m15_bruto = _fetch_bybit_klines_historico(symbol, '15', dias_historico + 3)
    m5_bruto = _fetch_bybit_klines_historico(symbol, '5', dias_historico + 2)
    h1_bruto = _fetch_bybit_klines_historico(symbol, '60', dias_historico + 3)

    d1, validacao_d1 = _validar_e_limpar_candles(d1_bruto, 'D')
    m15, validacao_m15 = _validar_e_limpar_candles(m15_bruto, '15')
    m5, validacao_m5 = _validar_e_limpar_candles(m5_bruto, '5')
    h1, validacao_h1 = _validar_e_limpar_candles(h1_bruto, '60')

    if len(d1) < 15 or len(m5) < 100 or len(h1) < 20:
        return {
            'erro': f'dados históricos insuficientes pra {pair} (D1={len(d1)}, M5={len(m5)}, H1={len(h1)})',
            'validacao_d1': validacao_d1, 'validacao_m5': validacao_m5, 'validacao_h1': validacao_h1,
        }

    # ── db_file temporário e descartável — NUNCA o banco de produção ──
    tmp_fd, tmp_db_path = tempfile.mkstemp(suffix='.db', prefix=f'shadow_gates_reprovados_{pair}_')
    os.close(tmp_fd)

    todas_ocorrencias_brutas = 0

    # ── Agregado do diagnóstico SFP (Casos A-E), item aprovado do
    # ticket — conta em qual caso cada ciclo caiu, pra TODO ciclo. ──
    sfp_diagnostico_agregado = {}

    # ── Correlação causal diag.caso vs decisão real (item aprovado) ──
    correlacao_diag_vs_real = {
        'D_e_real_confirma': 0, 'D_mas_real_NAO_confirma': 0, 'real_confirma_mas_diag_NAO_e_D': 0,
    }
    motivos_divergencia_D = {}
    tfs_divergencia_D = {}
    motivos_diag_quando_real_confirma = {}

    # ── Captura de texto bruto dos motivos não classificados (item
    # aprovado do ticket) — chave = texto exato do motivo, valor =
    # quantas vezes apareceu. Puramente observacional. ──
    motivos_nao_classificados_texto = {}
    sfp_rejeicao_fisica_motivos_texto = {}

    # ── DETALHE DOS GATES A-G — instrumentação pura. resultado['gates']
    # já é retornado por process_pair_gates_vortex() sem alteração
    # nenhuma (é o mesmo dict usado pra decidir SIGNAL_DISPARADO vs
    # gates_reprovados em produção); aqui só LEMOS e agregamos, não
    # recalculamos nem alteramos nenhum gate. ──
    gates_avaliacoes_brutas = []  # 1 entrada por CICLO (não dedupado) em que os gates foram avaliados
    gates_agregado = {
        'A_MTF_ALIGNMENT': {'aprovados': 0, 'reprovados': 0},
        'B_TRIGGER': {'aprovados': 0, 'reprovados': 0},
        'C_MONTE_CARLO': {'aprovados': 0, 'reprovados': 0},
        'D_SMC_QUALITY': {'aprovados': 0, 'reprovados': 0},
        'E_MIN_RR': {'aprovados': 0, 'reprovados': 0},
        'F_ICHIMOKU_INFO': {'aprovados': 0, 'reprovados': 0},
        'G_PREMIUM_DISCOUNT': {'aprovados': 0, 'reprovados': 0},
    }
    combinacoes_reprovacao = {}  # tupla ordenada de gates que falharam -> contagem

    # ── Agregado por OPERANDO isolado dos gates A/B/C (item aprovado
    # do ticket) — cada chave é uma comparação/leitura ATÔMICA de um
    # único operando cru, nunca uma combinação AND/OR de vários. Serve
    # pra responder "quantas vezes cada pedacinho individual bateu",
    # sem nunca reimplementar a lógica composta do gate. ──
    from collections import defaultdict
    operandos_agregado = {
        'A': {
            'bias_d1_igual_direcao_permitida': defaultdict(int),
            'mtf_alinhado': defaultdict(int),
            'mss_direcao_igual_direcao_permitida': defaultdict(int),
            'mss_presente': defaultdict(int),
        },
        'B': {
            'sfp_presente': defaultdict(int),
            'mss_presente': defaultdict(int),
            'entry_zone_presente': defaultdict(int),
            'atr_ok': defaultdict(int),
        },
        'C': {
            'prob_acerto_existe': defaultdict(int),
            'prob_acerto_atinge_threshold': defaultdict(int),
            'monte_carlo_gate_ativo': defaultdict(int),
        },
    }

    # ── FUNIL — instrumentação pura, só contagem. Não decide nada, não
    # altera process_pair_gates_vortex() nem nenhuma função de produção.
    # Cada chamada cai em EXATAMENTE uma categoria (retorno antecipado
    # mutuamente exclusivo no código real), então a contagem por
    # categoria JÁ É o funil — não precisa reimplementar a lógica de
    # decisão pra saber onde cada candle "morreu". ──
    funil = {
        'candles_m5_processados': 0,
        'staleness_rejeitados': 0,
        'candles_insuficientes': 0,
        'hora_toxica': 0,
        'sem_bias': 0,
        'bias_acima_midnight_open': 0,
        'bias_abaixo_midnight_open': 0,
        'bias_neutro': 0,  # sempre 0 nesta versão do Kairos — compute_bias_midnight_open_estrito() é binário (alta/baixa), nunca devolve neutro; campo mantido só pra satisfazer o formato do relatório pedido, com esta nota explícita
        'sfp_candidatos': 0,   # == bias_avaliado (todo ciclo que passou do bias tenta achar SFP)
        'sfp_confirmados': 0,  # tf_sfp_usado presente no resultado, independente do que acontece depois
        'sfp_repetido_cluster': 0,
        'padrao_fraco': 0,
        'mss_confirmados': 0,
        'sem_mss': 0,
        'fvg_encontrados': 0,
        'sem_fvg': 0,
        'risco_invalido': 0,
        'setups_com_entry_sl_tp': 0,
        'gates_avaliados': 0,
        'gates_reprovados': 0,
        'sinais_aprovados': 0,
        'em_cooldown': 0,
        'outro_motivo_nao_classificado': 0,
        'sfp_rejeicao_fisica_insuficiente': 0,
    }
    # Validação causal do H1 — prova que nenhum candle H1 futuro (t >
    # ts_corte) jamais entrou em candles_por_tf['H1'] em nenhum ciclo.
    violacoes_causais_h1 = 0
    amostras_causais_h1 = []  # guarda só os primeiros/últimos ciclos, pra não inflar o relatório

    try:
        for i in range(len(m5)):
            ts_corte = m5[i]['t']
            candles_por_tf = _montar_candles_por_tf_ate(d1, None, h1, m15, m5[:i + 1], None, ts_corte)
            funil['candles_m5_processados'] += 1

            # ── Validação causal do H1 (item 1 do ticket) ──
            h1_visivel = candles_por_tf.get('H1') or []
            ultimo_h1_t = h1_visivel[-1]['t'] if h1_visivel else None
            if ultimo_h1_t is not None and ultimo_h1_t > ts_corte:
                violacoes_causais_h1 += 1
            if i < 3 or i >= len(m5) - 3:
                amostras_causais_h1.append({
                    'ts_corte': ts_corte, 'ultimo_h1_t': ultimo_h1_t,
                    'quantidade_h1_visivel': len(h1_visivel),
                })

            try:
                agora_ts_historico = (ts_corte / 1000) + GATES_REPROVADOS_EXEC_TF_SEG
                resultado = process_pair_gates_vortex(
                    tmp_db_path, pair, candles_por_tf, exec_tf_label='M5',
                    agora_ts=agora_ts_historico, debug_gates=True,
                )
            except Exception:
                continue

            motivo = resultado.get('motivo') or ''

            # ── DIAGNÓSTICO SFP (Casos A-E) — item aprovado do ticket.
            # Captura o diag já calculado pela produção (reaproveitando
            # _diagnostico_detalhado_sfp, existente e não alterada) pra
            # TODO ciclo, não só os que chegam a gates_reprovados. Serve
            # pra descobrir onde o funil SFP realmente afunila:
            # nunca_tocou / tocou_sem_reclaim / breakout / confirmado /
            # sem_dados. Quando o ciclo nem chegou a calcular SFP
            # (candles insuficientes, staleness, hora tóxica, sem bias),
            # não existe diag — contabiliza como 'nao_disponivel'. ──
            sfp_diag = resultado.get('_debug_sfp_diagnostico')
            if sfp_diag:
                caso = sfp_diag.get('caso') or 'caso_desconhecido'
                sfp_diagnostico_agregado[caso] = sfp_diagnostico_agregado.get(caso, 0) + 1
            else:
                caso = None
                sfp_diagnostico_agregado['nao_disponivel'] = sfp_diagnostico_agregado.get('nao_disponivel', 0) + 1

            # ── CORRELAÇÃO CAUSAL diag.caso vs decisão real (item
            # aprovado do ticket) — cruza, no MESMO ciclo, o Caso do
            # diagnóstico observacional (single-tf, primeiro toque) com
            # o resultado real da cascata M15→M5→M1
            # (validar_sfp_cascata_tf, via tf_sfp_usado != None). Não
            # recalcula nada — só lê 2 sinais já existentes no mesmo
            # resultado. Isso explica objetivamente por que
            # diagnostico_sfp_casos.D_sfp_confirmado (por ciclo, 1
            # timeframe fixo, para no primeiro toque) diverge de
            # funil.sfp_confirmados (cascata real M15→M5→M1, continua
            # escaneando até achar o SFP mais recente ou um breakout
            # mais à frente). Roda pra TODO ciclo, independente de ter
            # diag disponível ou não. ──
            real_sfp_confirmado_neste_ciclo = resultado.get('tf_sfp_usado') is not None
            tf_real_usado = resultado.get('tf_sfp_usado')
            if caso == 'D_sfp_confirmado':
                if real_sfp_confirmado_neste_ciclo:
                    correlacao_diag_vs_real['D_e_real_confirma'] += 1
                else:
                    correlacao_diag_vs_real['D_mas_real_NAO_confirma'] += 1
                    motivo_quando_diverge = motivo or 'motivo_vazio'
                    motivos_divergencia_D[motivo_quando_diverge] = motivos_divergencia_D.get(motivo_quando_diverge, 0) + 1
                    tfs_divergencia_D[str(tf_real_usado)] = tfs_divergencia_D.get(str(tf_real_usado), 0) + 1
            elif real_sfp_confirmado_neste_ciclo:
                # Real confirma mas diag (single-tf, primeiro toque) não viu Caso D
                correlacao_diag_vs_real['real_confirma_mas_diag_NAO_e_D'] += 1
                motivos_diag_quando_real_confirma[str(caso)] = motivos_diag_quando_real_confirma.get(str(caso), 0) + 1

            # ── DETALHE DOS GATES A-G — captura o dict já retornado por
            # process_pair_gates_vortex() (não alterado, não recalculado).
            # Roda pra qualquer ciclo em que os gates foram avaliados
            # (gates_reprovados, sinal_disparado ou em_cooldown), não só
            # nos reprovados, pra ter o quadro completo. ──
            gates_dict = resultado.get('gates')
            if gates_dict:
                gates_reprovados_lista = [g for g, ok in gates_dict.items() if not ok]
                # ── Valores REAIS: resultado['entry']/['sl']/etc. ficam
                # None no branch de gates_reprovados (produção só
                # escreve esses campos no branch de sucesso). Os
                # valores locais verdadeiros vêm de
                # resultado['_debug_gate_inputs'], só presente porque
                # chamamos com debug_gates=True (opt-in, produção nunca
                # ativa isso). ──
                debug_info = resultado.get('_debug_gate_inputs') or {}
                entry_real = debug_info.get('entry_local')
                sl_real = debug_info.get('sl_local')
                tp1_real = debug_info.get('tp1_local')
                tp2_real = debug_info.get('tp2_local')
                direcao_real = debug_info.get('direcao_local')
                gate_a_op = debug_info.get('gate_a_operandos') or {}
                gate_b_op = debug_info.get('gate_b_operandos') or {}
                gate_c_op = debug_info.get('gate_c_operandos') or {}
                gates_avaliacoes_brutas.append({
                    'ts_corte': ts_corte, 'idx_m5': i, 'direcao': direcao_real,
                    'entry': entry_real, 'sl': sl_real,
                    'tp1': tp1_real, 'tp2': tp2_real,
                    'gates': dict(gates_dict),
                    'gates_reprovados_lista': gates_reprovados_lista,
                    'motivo': motivo,
                    'gate_a_operandos': gate_a_op,
                    'gate_b_operandos': gate_b_op,
                    'gate_c_operandos': gate_c_op,
                })
                # ── Agregado por OPERANDO (não por combinação lógica) —
                # cada linha abaixo é uma COMPARAÇÃO DIRETA e isolada
                # entre 2 valores crus já capturados, sem recombinar
                # AND/OR de várias condições. Serve só pra contar
                # quantas vezes cada operando individual bateu/não
                # bateu, não pra decidir nada. ──
                if gate_a_op:
                    _cmp_bias = gate_a_op.get('bias_d1') == gate_a_op.get('direcao_permitida')
                    _cmp_mtf = bool(gate_a_op.get('mtf_alinhado'))
                    _cmp_mss_dir = (gate_a_op.get('mss_direcao') == gate_a_op.get('direcao_permitida'))
                    operandos_agregado['A']['bias_d1_igual_direcao_permitida'][_cmp_bias] += 1
                    operandos_agregado['A']['mtf_alinhado'][_cmp_mtf] += 1
                    operandos_agregado['A']['mss_direcao_igual_direcao_permitida'][_cmp_mss_dir] += 1
                    operandos_agregado['A']['mss_presente'][bool(gate_a_op.get('mss_presente'))] += 1
                if gate_b_op:
                    operandos_agregado['B']['sfp_presente'][bool(gate_b_op.get('sfp'))] += 1
                    operandos_agregado['B']['mss_presente'][bool(gate_b_op.get('mss'))] += 1
                    operandos_agregado['B']['entry_zone_presente'][bool(gate_b_op.get('entry_zone'))] += 1
                    operandos_agregado['B']['atr_ok'][bool(gate_b_op.get('atr_ok'))] += 1
                if gate_c_op:
                    _prob = gate_c_op.get('prob_acerto')
                    _threshold = gate_c_op.get('gate_c_min_prob_exigido')
                    operandos_agregado['C']['prob_acerto_existe'][_prob is not None] += 1
                    if _prob is not None and _threshold is not None:
                        operandos_agregado['C']['prob_acerto_atinge_threshold'][_prob >= _threshold] += 1
                    operandos_agregado['C']['monte_carlo_gate_ativo'][bool(gate_c_op.get('monte_carlo_gate_ativo'))] += 1
                for g, ok in gates_dict.items():
                    if g not in gates_agregado:
                        gates_agregado[g] = {'aprovados': 0, 'reprovados': 0}
                    if ok:
                        gates_agregado[g]['aprovados'] += 1
                    else:
                        gates_agregado[g]['reprovados'] += 1
                if gates_reprovados_lista:
                    combo = tuple(sorted(gates_reprovados_lista))
                    combinacoes_reprovacao[combo] = combinacoes_reprovacao.get(combo, 0) + 1

            # ── Tally do funil (item 3 do ticket) — leitura pura dos
            # campos já retornados por process_pair_gates_vortex(), sem
            # nenhuma lógica de decisão nova. ──
            if 'candles insuficientes' in motivo:
                funil['candles_insuficientes'] += 1
            elif 'dados obsoletos' in motivo:
                funil['staleness_rejeitados'] += 1
            elif 'horário tóxico' in motivo:
                funil['hora_toxica'] += 1
            elif 'calcular Midnight Open' in motivo:
                funil['sem_bias'] += 1
            else:
                # bias foi avaliado com sucesso nesse ciclo
                if 'ACIMA_MIDNIGHT_OPEN' in motivo:
                    funil['bias_acima_midnight_open'] += 1
                elif 'ABAIXO_MIDNIGHT_OPEN' in motivo:
                    funil['bias_abaixo_midnight_open'] += 1
                funil['sfp_candidatos'] += 1

                sfp_confirmado_neste_ciclo = resultado.get('tf_sfp_usado') is not None
                if sfp_confirmado_neste_ciclo:
                    funil['sfp_confirmados'] += 1

                if 'breakout_cancela_analise' in motivo:
                    pass  # sem_sfp — bias inverteu antes do SFP se formar, já contado em sfp_candidatos
                elif 'sem_sfp_ainda' in motivo or 'sem_liquidez_mapeada' in motivo or 'sem_candles_sfp' in motivo:
                    pass  # idem — SFP nunca confirmou
                elif 'SFP repetido dentro do cluster' in motivo:
                    funil['sfp_repetido_cluster'] += 1
                elif 'rejeição física insuficiente no candle do SFP' in motivo:
                    # ── item aprovado do ticket: promove o motivo já
                    # confirmado pelo dado real (36/36 do LINK) de
                    # observacional pra contador oficial do funil.
                    # Origem: process_pair_gates_vortex(), no if not
                    # rejeicao_ok: (não alterado). Estágio real: entre
                    # SFP confirmado e MSS. ──
                    funil['sfp_rejeicao_fisica_insuficiente'] += 1
                    sfp_rejeicao_fisica_motivos_texto[motivo] = sfp_rejeicao_fisica_motivos_texto.get(motivo, 0) + 1
                elif 'padrão de rejeição fraco' in motivo:
                    funil['padrao_fraco'] += 1
                elif 'pra validar MSS' in motivo or 'sem MSS de corpo forte' in motivo:
                    funil['sem_mss'] += 1
                elif 'sem FVG real após a expansão' in motivo:
                    funil['mss_confirmados'] += 1
                    funil['sem_fvg'] += 1
                elif 'risco calculado inválido' in motivo:
                    funil['mss_confirmados'] += 1
                    funil['fvg_encontrados'] += 1
                    funil['risco_invalido'] += 1
                elif 'falhou nos gates' in motivo:
                    funil['mss_confirmados'] += 1
                    funil['fvg_encontrados'] += 1
                    funil['setups_com_entry_sl_tp'] += 1
                    funil['gates_avaliados'] += 1
                    funil['gates_reprovados'] += 1
                elif motivo == 'entrada_confirmada' or 'entrada_confirmada, mas em cooldown' in motivo:
                    funil['mss_confirmados'] += 1
                    funil['fvg_encontrados'] += 1
                    funil['setups_com_entry_sl_tp'] += 1
                    funil['gates_avaliados'] += 1
                    funil['sinais_aprovados'] += 1
                    if 'em cooldown' in motivo:
                        funil['em_cooldown'] += 1
                elif motivo:
                    funil['outro_motivo_nao_classificado'] += 1
                    # ── item aprovado do ticket: captura o TEXTO EXATO
                    # do motivo quando ele não bate com nenhuma
                    # palavra-chave conhecida. Não altera a
                    # classificação, não decide nada — só preserva o
                    # dado bruto pra investigação, em vez de descartá-lo. ──
                    motivos_nao_classificados_texto[motivo] = motivos_nao_classificados_texto.get(motivo, 0) + 1

            if 'falhou nos gates' not in motivo:
                continue

            todas_ocorrencias_brutas += 1
    finally:
        try:
            os.remove(tmp_db_path)
        except Exception:
            pass

    # ── IDENTIDADE TEMPORAL + CLUSTERS (item aprovado do ticket) ──
    # Filtra só as avaliações que reprovaram nos gates (mesmo universo
    # de todas_ocorrencias_brutas), já em ordem cronológica (o loop
    # acima é sequencial). NÃO deduplica por janela de candles — só
    # agrupa avaliações CONSECUTIVAS que têm entry+sl+direção
    # IDÊNTICOS entre si (ver _reconstruir_identidade_temporal_gates).
    avaliacoes_gates_reprovados_brutas = [
        av for av in gates_avaliacoes_brutas if 'falhou nos gates' in av['motivo']
    ]
    avaliacoes_com_delta, clusters_por_identidade = _reconstruir_identidade_temporal_gates(
        avaliacoes_gates_reprovados_brutas
    )

    # ── SIMULAÇÃO DE CENÁRIOS (item aprovado do ticket) — puramente
    # observacional. Não altera nenhum gate real, não recalcula nada,
    # só recombina os booleanos já decididos pela produção de formas
    # alternativas, e mede o resultado real (TP/SL, sem lookahead) de
    # cada cenário. ──
    cenarios_simulados = _simular_cenarios_gates(avaliacoes_gates_reprovados_brutas, m5, pair)

    # ── "oportunidades" pra resolução de TP/SL downstream: 1
    # REPRESENTANTE por cluster de identidade real (a primeira
    # avaliação de cada cluster), não as N reavaliações do mesmo
    # cluster. Clusters sem identidade válida (entry/sl ausentes) são
    # excluídos da resolução de TP/SL e reportados à parte. ──
    oportunidades = []
    clusters_sem_identidade_valida = 0
    for idx_cluster, c in enumerate(clusters_por_identidade):
        if not c['identidade_valida']:
            clusters_sem_identidade_valida += 1
            continue
        primeira_av = next(
            av for av in avaliacoes_gates_reprovados_brutas
            if av['ts_corte'] == c['primeiro_ts'] and av['direcao'] == c['direcao']
            and av['entry'] == c['entry'] and av['sl'] == c['sl']
        )
        oportunidades.append({
            'pair': pair, 'ts_evento': c['primeiro_ts'], 'direcao': c['direcao'],
            'entry': c['entry'], 'sl': c['sl'], 'tp1': c['tp1'], 'tp2': c['tp2'],
            'idx_m5': primeira_av['idx_m5'], 'cluster_tamanho': c['tamanho'],
        })

    # ── "oportunidades" (representante por cluster) fica pronta, mas
    # a resolução de TP/SL/win-rate downstream é SUSPENSA nesta
    # rodada, por pedido explícito: ainda não decidimos a regra de
    # dedup definitiva, só reconstruímos a identidade temporal + os
    # clusters por igualdade real. clusters_por_identidade continua
    # disponível no relatório como dado observacional. ──

    # ── Resolver TP/SL/AMBIGUO/NENHUM nos 3 horizontes, sem lookahead ──
    # SUSPENSO nesta rodada (ver nota acima) — lista fica vazia de
    # propósito, sem consumir 'oportunidades' ainda.
    resultados_por_horizonte = {h: [] for h in GATES_REPROVADOS_HORIZONTES_CANDLES}
    for op in []:
        if op['entry'] is None or op['sl'] is None:
            continue
        idx = op['idx_m5']
        candles_futuros = m5[idx + 1:]
        for h in GATES_REPROVADOS_HORIZONTES_CANDLES:
            res = _resolver_tp_sl_futuro(
                candles_futuros, op['direcao'], op['entry'], op['sl'], op['tp1'], op['tp2'], h,
            )
            resultados_por_horizonte[h].append({**op, **res})

    relatorio_por_horizonte = {}
    for h, lista in resultados_por_horizonte.items():
        relatorio_por_horizonte[f'{h}_candles_M5'] = _agregar_relatorio_gates_reprovados(lista)

    n_long = sum(1 for op in oportunidades if op.get('direcao') == 'alta')
    n_short = sum(1 for op in oportunidades if op.get('direcao') == 'baixa')

    primeiro_ts_m5 = m5[0]['t'] if m5 else None
    ultimo_ts_m5 = m5[-1]['t'] if m5 else None
    duracao_horas = round((ultimo_ts_m5 - primeiro_ts_m5) / 3600000, 2) if (primeiro_ts_m5 and ultimo_ts_m5) else None
    duracao_dias = round(duracao_horas / 24, 2) if duracao_horas is not None else None

    # Confirmação empírica do intervalo real (mediana da diferença entre
    # candles consecutivos) — não assume, mede o dado de verdade.
    intervalo_medido_seg = None
    if len(m5) >= 2:
        diffs = sorted((m5[i]['t'] - m5[i - 1]['t']) / 1000 for i in range(1, len(m5)))
        intervalo_medido_seg = diffs[len(diffs) // 2]

    return {
        'pair': pair, 'dias_historico': dias_historico,
        'dados_historicos': {
            'candles_d1': len(d1), 'candles_m5': len(m5), 'candles_h1': len(h1),
            'primeiro_ts_m5': primeiro_ts_m5, 'ultimo_ts_m5': ultimo_ts_m5,
            'duracao_coberta_horas': duracao_horas, 'duracao_coberta_dias': duracao_dias,
            'intervalo_m5_medido_segundos': intervalo_medido_seg,
            'intervalo_m5_confirmado_5min': intervalo_medido_seg == 300 if intervalo_medido_seg is not None else None,
            'gaps_m5': validacao_m5.get('gaps'), 'candle_em_formacao_removido_m5': validacao_m5.get('candle_em_formacao_removido'),
        },
        'validacao_dados': {'D1': validacao_d1, 'M5': validacao_m5, 'H1': validacao_h1},
        'oportunidades_brutas_antes_dedup': todas_ocorrencias_brutas,
        'oportunidades_unicas_apos_dedup': None,
        'clusters_por_identidade_real_preliminar': len(clusters_por_identidade),
        'clusters_sem_identidade_valida': clusters_sem_identidade_valida,
        'reavaliacoes_repetidas': None,
        'taxa_repeticao': None,
        'oportunidades_long': n_long, 'oportunidades_short': n_short,
        'nota_metodologica': (
            'Réplica exata da lógica de produção: process_pair_gates_vortex() foi chamado sem '
            'alteração, contra db_file temporário/descartável, andando candle a candle no '
            'histórico real (sem lookahead — cada chamada só enxerga candles com t <= ts_corte). '
            'ATENÇÃO: nesta rodada, a regra de dedup/oportunidades_unicas AINDA NÃO foi decidida '
            '— "oportunidades_unicas_apos_dedup" fica None de propósito. O que existe é '
            '"clusters_por_identidade_real_preliminar": um agrupamento OBSERVACIONAL de '
            'avaliações CONSECUTIVAS com entry+sl+direção IDÊNTICOS entre si (não uma janela de '
            'candles arbitrária) — ver "identidade_temporal_eventos" e "clusters_detalhe" para os '
            'dados crus. O cálculo de TP/SL/win-rate por horizonte foi SUSPENSO nesta rodada, até '
            'a regra de dedup ser aprovada com base nesses dados. Os valores de entry/sl/tp1/tp2 '
            'usados aqui são os LOCAIS reais (via debug_gates=True), não os do dict público de '
            'process_pair_gates_vortex() — que ficam None no branch de gates_reprovados por '
            'característica já existente da função (resultado.update() só roda no branch de '
            'sucesso), não por falha desta instrumentação. sfp_breakout_cancelado NÃO É '
            'backtestável nesta metodologia (sem entry/sl coerente) e não aparece neste relatório.'
        ),
        'validacao_causal_h1': {
            'violacoes_causais_h1': violacoes_causais_h1,
            'h1_100_por_cento_causal': violacoes_causais_h1 == 0,
            'nota': (
                'violacoes_causais_h1 conta quantos ciclos tiveram o último candle H1 visível '
                'com timestamp POSTERIOR ao ts_corte do ciclo (o que seria lookahead). Deve ser '
                'sempre 0 — _montar_candles_por_tf_ate() filtra H1 por t <= ts_corte_ms, igual '
                'aos demais timeframes.'
            ),
            'amostras_primeiros_e_ultimos_ciclos': amostras_causais_h1,
        },
        'funil': funil,
        'gates_detalhe': {
            'avaliacoes_brutas': gates_avaliacoes_brutas,
            'total_avaliacoes': len(gates_avaliacoes_brutas),
            'agregado_por_gate': gates_agregado,
            'combinacao_mais_frequente_reprovacao': (
                {'gates_reprovados': list(max(combinacoes_reprovacao.items(), key=lambda x: x[1])[0]),
                 'ocorrencias': max(combinacoes_reprovacao.items(), key=lambda x: x[1])[1]}
                if combinacoes_reprovacao else None
            ),
            'todas_combinacoes_reprovacao': [
                {'gates_reprovados': list(k), 'ocorrencias': v}
                for k, v in sorted(combinacoes_reprovacao.items(), key=lambda x: -x[1])
            ],
            'operandos_agregado_por_subcondicao': {
                gate_letra: {
                    nome_operando: {
                        'true': contagem.get(True, 0),
                        'false': contagem.get(False, 0),
                    }
                    for nome_operando, contagem in operandos.items()
                }
                for gate_letra, operandos in operandos_agregado.items()
            },
        },
        'identidade_temporal_eventos': {
            'nota': (
                'Cada item = 1 avaliação BRUTA (não dedupada) que reprovou nos gates, em ordem '
                'cronológica, com entry/sl/tp1/tp2/direção REAIS (valores locais capturados via '
                'debug_gates=True, não os campos públicos que ficam None nesse branch).'
            ),
            'total_avaliacoes_gates_reprovados': len(avaliacoes_com_delta),
            'avaliacoes': avaliacoes_com_delta,
        },
        'clusters_detalhe': {
            'nota': (
                'Cada cluster = sequência de avaliações CONSECUTIVAS com entry+sl+direção '
                'IDÊNTICOS entre si. NÃO é uma janela de candles arbitrária — é igualdade real '
                'entre vizinhos diretos no tempo. Quando a identidade muda (mesmo que só um '
                'gate a mais reprove, por exemplo), o cluster fecha e um novo começa. Dado '
                'observacional/preliminar — a decisão de dedup definitivo ainda não foi tomada.'
            ),
            'total_clusters': len(clusters_por_identidade),
            'clusters_sem_identidade_valida': clusters_sem_identidade_valida,
            'clusters': clusters_por_identidade,
        },
        'diagnostico_sfp_casos': {
            'nota': (
                'Diagnóstico causal (Casos A-E, reaproveitando _diagnostico_detalhado_sfp() já '
                'existente e não alterada) de TODO ciclo, não só os que chegam a gates_reprovados. '
                'A_nunca_tocou = preço nunca varreu o nível de liquidez de referência. '
                'B_tocou_sem_reclaim = varreu mas ainda não fechou de volta (sweep em andamento). '
                'C_breakout_confirmado = fechou além do nível (invalida o SFP daquele lado). '
                'D_sfp_confirmado = varreu e fechou de volta (SFP válido). '
                'E_sem_dados / E_sem_candles_apos_cutoff = sem liquidez mapeada ou sem candles '
                'suficientes pra avaliar. nao_disponivel = ciclo nem chegou a essa etapa (bloqueado '
                'antes, por staleness/hora tóxica/candles insuficientes/sem bias).'
            ),
            'contagem_por_caso': sfp_diagnostico_agregado,
            'correlacao_diag_vs_decisao_real': {
                'nota': (
                    'Cruza, no MESMO ciclo, o Caso do diagnóstico (single-timeframe, para no '
                    'primeiro toque) com a decisão real (cascata M15→M5→M1 completa, continua '
                    'escaneando até achar o SFP mais recente ou um breakout mais à frente na '
                    'janela). D_e_real_confirma = os dois concordam. D_mas_real_NAO_confirma = '
                    'diag viu reclaim no primeiro toque, mas a decisão real (que escaneia até o '
                    'fim da janela) achou outra coisa depois — motivo real registrado em '
                    'motivos_divergencia_D. real_confirma_mas_diag_NAO_e_D = a decisão real '
                    'confirmou mas o diag (single-tf fixo) não bateu com Caso D nesse ciclo — '
                    'geralmente porque tf_sfp_usado real foi diferente do timeframe fixo do diag.'
                ),
                'contagem': correlacao_diag_vs_real,
                'motivos_reais_quando_D_diverge': motivos_divergencia_D,
                'timeframes_reais_quando_D_diverge': tfs_divergencia_D,
                'casos_diag_quando_real_confirma_mas_nao_e_D': motivos_diag_quando_real_confirma,
            },
            'motivos_nao_classificados_texto_exato': {
                'nota': (
                    'Item aprovado do ticket — captura o TEXTO EXATO de resultado["motivo"] toda '
                    'vez que um SFP real confirmado (tf_sfp_usado != None) cai no bucket '
                    'outro_motivo_nao_classificado do funil, ou seja, não bateu com nenhuma '
                    'palavra-chave conhecida do classificador. Puramente observacional — não '
                    'altera a classificação nem nenhuma decisão, só preserva o dado bruto que '
                    'antes era descartado.'
                ),
                'distribuicao': motivos_nao_classificados_texto,
                'total': sum(motivos_nao_classificados_texto.values()),
            },
            'sfp_rejeicao_fisica_insuficiente_detalhe': {
                'nota': (
                    'Distribuição do texto exato (incluindo padrão=X) dos ciclos contados em '
                    'funil["sfp_rejeicao_fisica_insuficiente"] — mesmo motivo já lido, sem '
                    'recalcular nada. Estágio: entre SFP confirmado e MSS. Threshold real '
                    '(rejeicao_ok >= 0.25) permanece intocado, isto é só telemetria.'
                ),
                'distribuicao': sfp_rejeicao_fisica_motivos_texto,
                'total': sum(sfp_rejeicao_fisica_motivos_texto.values()),
            },
        },
        'simulacao_cenarios_gates': {
            'nota': (
                'SIMULAÇÃO/REPLAY PURO — nenhum gate real foi alterado, nenhuma regra de '
                'produção foi tocada. Cada cenário recombina os booleanos JÁ DECIDIDOS pela '
                'produção (resultado["gates"], sem recalcular nenhum gate) de uma forma '
                'alternativa, e mede TP/SL real nos candles futuros (horizonte de 20 candles '
                'M5, sem lookahead). "atual" deve sempre dar 0 entradas neste relatório, porque '
                'só avaliações que JÁ reprovaram em produção entram aqui — serve de controle/'
                'sanity-check da simulação, não é um resultado novo.'
            ),
            'cenarios': cenarios_simulados,
        },
        'por_horizonte': relatorio_por_horizonte,
    }


def _agregar_relatorio_gates_reprovados(lista_resultados):
    if not lista_resultados:
        return {'amostra': 0, 'nota': 'AMOSTRA INSUFICIENTE PARA CONCLUSÃO'}

    n = len(lista_resultados)
    n_tp1 = sum(1 for r in lista_resultados if r['resultado'] == 'TP1')
    n_tp2 = sum(1 for r in lista_resultados if r['resultado'] == 'TP2')
    n_sl = sum(1 for r in lista_resultados if r['resultado'] == 'SL')
    n_ambiguo = sum(1 for r in lista_resultados if r['resultado'] == 'AMBIGUO')
    n_nenhum = sum(1 for r in lista_resultados if r['resultado'] == 'NENHUM')

    n_win = n_tp1 + n_tp2
    n_resolvidos = n_win + n_sl
    win_rate = round(100 * n_win / n_resolvidos, 1) if n_resolvidos else None
    loss_rate = round(100 - win_rate, 1) if win_rate is not None else None

    mfes = sorted(r['mfe_pct'] for r in lista_resultados)
    maes = sorted(r['mae_pct'] for r in lista_resultados)
    mfe_medio = round(sum(mfes) / len(mfes), 4) if mfes else None
    mae_medio = round(sum(maes) / len(maes), 4) if maes else None
    mfe_mediano = _percentil(mfes, 50)
    mae_mediano = _percentil(maes, 50)

    # Expectancy com RR real do próprio setup (TP1), sem inventar RR
    rrs_win = []
    for r in lista_resultados:
        if r['resultado'] in ('TP1', 'SL') and r['entry'] is not None and r['sl'] is not None and r['tp1'] is not None:
            risco = abs(r['entry'] - r['sl'])
            retorno_tp1 = abs(r['tp1'] - r['entry'])
            if risco > 0:
                rrs_win.append(retorno_tp1 / risco)
    rr_medio_tp1 = round(sum(rrs_win) / len(rrs_win), 2) if rrs_win else None
    expectancy_tp1 = None
    if win_rate is not None and rr_medio_tp1 is not None:
        wr = win_rate / 100
        expectancy_tp1 = round((wr * rr_medio_tp1) - (1 - wr), 4)

    por_direcao = {}
    for direcao in ('alta', 'baixa'):
        sub = [r for r in lista_resultados if r.get('direcao') == direcao]
        if not sub:
            continue
        sub_win = sum(1 for r in sub if r['resultado'] in ('TP1', 'TP2'))
        sub_sl = sum(1 for r in sub if r['resultado'] == 'SL')
        sub_resolvidos = sub_win + sub_sl
        por_direcao[direcao] = {
            'eventos': len(sub), 'win': sub_win, 'loss': sub_sl,
            'win_rate_pct': round(100 * sub_win / sub_resolvidos, 1) if sub_resolvidos else None,
        }

    por_pair = {}
    for r in lista_resultados:
        p = r['pair']
        por_pair.setdefault(p, {'eventos': 0, 'win': 0, 'loss': 0})
        por_pair[p]['eventos'] += 1
        if r['resultado'] in ('TP1', 'TP2'):
            por_pair[p]['win'] += 1
        elif r['resultado'] == 'SL':
            por_pair[p]['loss'] += 1
    for p, v in por_pair.items():
        resolv = v['win'] + v['loss']
        v['win_rate_pct'] = round(100 * v['win'] / resolv, 1) if resolv else None

    return {
        'amostra': n,
        'amostra_suficiente': n >= 30,
        'nota': None if n >= 30 else 'AMOSTRA INSUFICIENTE PARA CONCLUSÃO',
        'TP1': n_tp1, 'TP2': n_tp2, 'SL': n_sl, 'AMBIGUO': n_ambiguo, 'NENHUM': n_nenhum,
        'win_rate_pct': win_rate, 'loss_rate_pct': loss_rate,
        'ambiguidade_pct': round(100 * n_ambiguo / n, 1),
        'expirado_pct': round(100 * n_nenhum / n, 1),
        'rr_medio_tp1': rr_medio_tp1,
        'expectancy_tp1': expectancy_tp1,
        'mfe_medio_pct': mfe_medio, 'mae_medio_pct': mae_medio,
        'mfe_mediano_pct': mfe_mediano, 'mae_mediano_pct': mae_mediano,
        'por_direcao': por_direcao,
        'por_pair': por_pair,
    }


def diagnostico_independente_luxalgo(candles_swing, candles_internal):
    """
    Diagnóstico SOMENTE LEITURA (item aprovado do ticket) — avalia
    bias/zone/internal_choch/fvg de forma INDEPENDENTE, SEM
    short-circuit, pra separar CONDIÇÃO REAL de MOTIVO DE REJEIÇÃO DA
    ORDEM ATUAL. avaliar_vortex_decision_layer() usa short-circuit
    (retorna assim que zone_ok falha, nunca chega a testar CHoCH) —
    essa função aqui testa TODAS as condições sempre, mesmo quando uma
    anterior já falhou.

    Reaproveita exatamente as mesmas funções já existentes e testadas
    (compute_lux_structure_bias, compute_lux_premium_discount,
    classificar_zona_lux, compute_lux_internal_structure,
    find_open_fvgs_adaptive) — nenhuma delas é alterada. NÃO chama
    nem altera avaliar_vortex_decision_layer(), avaliar_legacy_
    decision_layer() ou qualquer função de decisão/produção. Não
    decide nada, só mede e devolve os 4 booleanos independentes.
    """
    resultado = {
        'bias': None, 'bias_ok': False,
        'zona': None, 'zone_ok': False,
        'internal_choch_exists': False, 'internal_choch_evento': None,
        'valid_fvg_exists': False, 'fvg_candidatos_n': 0,
    }

    bias = compute_lux_structure_bias(candles_swing, swing_size=50)
    resultado['bias'] = bias
    resultado['bias_ok'] = bias in ('alta', 'baixa')

    zona_calc = compute_lux_premium_discount(candles_swing, swing_size=50)
    preco_atual = candles_swing[-1]['c'] if candles_swing else None
    zona = classificar_zona_lux(preco_atual, zona_calc) if zona_calc and preco_atual is not None else None
    resultado['zona'] = zona
    if bias == 'alta':
        resultado['zone_ok'] = zona == 'discount'
    elif bias == 'baixa':
        resultado['zone_ok'] = zona == 'premium'
    else:
        resultado['zone_ok'] = False

    # ── SEM short-circuit — testa CHoCH interno independente do
    # resultado de zone_ok acima. ──
    eventos_internos = compute_lux_internal_structure(candles_internal, swing_size=5)
    choch_relevante = None
    if bias in ('alta', 'baixa'):
        for ev in reversed(eventos_internos):
            if ev['tipo'] == 'CHoCH' and ev['direcao'] == bias:
                choch_relevante = ev
                break
    resultado['internal_choch_exists'] = choch_relevante is not None
    resultado['internal_choch_evento'] = choch_relevante

    # ── SEM short-circuit — testa FVG independente de zone_ok e de
    # internal_choch_exists acima. ──
    fvgs = find_open_fvgs_adaptive(candles_internal)
    tipo_fvg_desejado = 'FVG_bullish' if bias == 'alta' else ('FVG_bearish' if bias == 'baixa' else None)
    candidatos = [f for f in fvgs if tipo_fvg_desejado and f['tipo'] == tipo_fvg_desejado]
    resultado['valid_fvg_exists'] = len(candidatos) > 0
    resultado['fvg_candidatos_n'] = len(candidatos)

    return resultado


def replay_comparativo_luxalgo(pair, dias_historico=7):
    """
    Item E do ticket — replay comparativo SOMENTE LEITURA entre 3
    caminhos, candle a candle, sem lookahead:
      LEGACY (funções legadas do Kairos) vs LUXALGO-COMPATIBLE
      (funções novas, isoladas) vs VORTEX_LAYER (composição das
      funções LuxAlgo).
    NÃO escreve em nenhuma tabela de produção (nem usa db_file — as
    3 camadas aqui são funções puras, sem persistência). NÃO chama
    process_pair_gates_vortex() nem process_pair_4camadas(). NÃO
    altera nenhum gate, SFP, MSS, SL ou TP existente.
    Mesmo mecanismo causal já testado em replay_gates_reprovados():
    cada ciclo só enxerga candles com t <= ts_corte (m5[:i+1]).
    """
    symbol_map = {
        'BTCUSD': 'BTCUSDT', 'ETHUSD': 'ETHUSDT', 'SOLUSD': 'SOLUSDT', 'XRPUSD': 'XRPUSDT',
        'LINKUSD': 'LINKUSDT', 'ADAUSD': 'ADAUSDT', 'AVAXUSD': 'AVAXUSDT', 'BNBUSD': 'BNBUSDT',
        'AAVEUSD': 'AAVEUSDT', 'NEARUSD': 'NEARUSDT', 'PENDLEUSD': 'PENDLEUSDT', 'INJUSD': 'INJUSDT',
        'ONDOUSD': 'ONDOUSDT',
    }
    symbol = symbol_map.get(pair.upper(), pair.upper().replace('USD', 'USDT'))

    m5_bruto = _fetch_bybit_klines_historico(symbol, '5', dias_historico + 2)
    m5, validacao_m5 = _validar_e_limpar_candles(m5_bruto, '5')

    MIN_CANDLES_SWING = 55  # swing_size(50) + folga mínima, mesma exigência de compute_lux_structure_events
    if len(m5) < MIN_CANDLES_SWING + 10:
        return {
            'erro': f'dados históricos insuficientes pra {pair} (M5={len(m5)}, mínimo necessário={MIN_CANDLES_SWING + 10})',
            'validacao_m5': validacao_m5,
        }

    total_candles = 0
    legacy_signals = 0          # ciclos onde legacy chegou a ter fvg_candidatos (mais perto de um "setup")
    luxalgo_valid_setups = 0    # ciclos onde luxalgo/vortex chegou a ter fvg_candidatos
    vortex_decisions_nao_unknown = 0  # ciclos onde vortex parou ANTES do bloqueio FVG_CHOCH_RELATION_UNKNOWN

    bias_divergences = 0
    zone_divergences = 0
    choch_divergences = 0
    fvg_divergences = 0

    rejection_reasons_legacy = {}
    rejection_reasons_luxalgo = {}

    eventos_divergentes = []  # lista de {'t', 'campo', 'legacy', 'luxalgo'}

    MAX_EVENTOS_DIVERGENTES_GUARDADOS = 200  # não deixa a resposta explodir de tamanho

    # ── Medição de idade do CHoCH (item aprovado do ticket) — só
    # medição, não define janela nenhuma. ──
    medicoes_idade_choch = []
    validacao_correspondencia_choch = {'ok': 0, 'divergente': 0}

    # ── Diagnóstico independente (item aprovado do ticket) — sem
    # short-circuit, mede as 4 condições sempre, separado do motivo de
    # rejeição da ordem atual (que usa short-circuit). ──
    diag_agregado = {
        'total_candles': 0,
        'bias_ok': 0, 'bias_fail': 0,
        'premium_discount': {'premium': 0, 'discount': 0, 'equilibrium': 0, 'nao_disponivel': 0},
        'zone_ok': 0, 'zone_fail': 0,
        'internal_choch_exists': 0, 'internal_choch_absent': 0,
        'valid_fvg_exists': 0, 'valid_fvg_absent': 0,
        'combinations': {
            'zone_fail_choch_exists': 0, 'zone_fail_choch_absent': 0,
            'zone_ok_choch_exists': 0, 'zone_ok_choch_absent': 0,
            'choch_exists_fvg_exists': 0, 'choch_exists_fvg_absent': 0,
        },
    }

    for i in range(MIN_CANDLES_SWING, len(m5)):
        ts_corte = m5[i]['t']
        # ── janela causal: só candles com t <= ts_corte, mesmo padrão
        # já testado em replay_gates_reprovados (_montar_candles_por_tf_ate) ──
        candles_ate_agora = m5[:i + 1]

        total_candles += 1

        try:
            r_legacy = avaliar_legacy_decision_layer(candles_ate_agora, candles_ate_agora)
        except Exception as e:
            r_legacy = {'decisao': 'ERRO', 'motivo_rejeicao': f'excecao: {e}', 'bias': None, 'zona': None, 'choch': None, 'fvg_candidatos': []}

        try:
            r_vortex = avaliar_vortex_decision_layer(candles_ate_agora, candles_ate_agora)
        except Exception as e:
            r_vortex = {'decisao': 'ERRO', 'motivo_rejeicao': f'excecao: {e}', 'bias': None, 'zona': None, 'internal_choch': None, 'fvg_candidatos': []}

        # ── Diagnóstico independente (item aprovado do ticket) — mesma
        # janela causal candles_ate_agora, sem short-circuit. ──
        try:
            diag = diagnostico_independente_luxalgo(candles_ate_agora, candles_ate_agora)
        except Exception:
            diag = None

        # ── Medição de idade do CHoCH (item aprovado do ticket) — só
        # roda quando diag existe e diag['internal_choch_exists'] é
        # True, mesma janela causal (candles_ate_agora), idx_atual =
        # último índice dessa janela (i, já que candles_ate_agora =
        # m5[:i+1]). Puramente aditivo, não altera decisão nenhuma. ──
        if diag and diag.get('internal_choch_exists') and diag.get('bias') in ('alta', 'baixa'):
            try:
                medicao = _selecionar_choch_para_medicao(candles_ate_agora, diag['bias'], i)
            except Exception:
                medicao = None
            if medicao:
                medicoes_idade_choch.append(medicao)
                # ── Validação pedida no ticket: confirma que o CHoCH
                # medido aqui é EXATAMENTE o mesmo que diag['internal_
                # choch_evento'] já reportou (mesma seleção, calculada
                # de forma independente pra cross-check). ──
                evento_diag = diag.get('internal_choch_evento') or {}
                bate = (
                    evento_diag.get('t') == medicao['choch_ts']
                    and evento_diag.get('index') == medicao['choch_index']
                )
                if bate:
                    validacao_correspondencia_choch['ok'] += 1
                else:
                    validacao_correspondencia_choch['divergente'] += 1

        if diag:
            diag_agregado['total_candles'] += 1
            if diag['bias_ok']:
                diag_agregado['bias_ok'] += 1
            else:
                diag_agregado['bias_fail'] += 1

            zona_diag = diag['zona']
            if zona_diag in ('premium', 'discount', 'equilibrium'):
                diag_agregado['premium_discount'][zona_diag] += 1
            else:
                diag_agregado['premium_discount']['nao_disponivel'] += 1

            if diag['zone_ok']:
                diag_agregado['zone_ok'] += 1
            else:
                diag_agregado['zone_fail'] += 1

            choch_existe = diag['internal_choch_exists']
            if choch_existe:
                diag_agregado['internal_choch_exists'] += 1
            else:
                diag_agregado['internal_choch_absent'] += 1

            fvg_existe = diag['valid_fvg_exists']
            if fvg_existe:
                diag_agregado['valid_fvg_exists'] += 1
            else:
                diag_agregado['valid_fvg_absent'] += 1

            # ── Combinações pedidas explicitamente no ticket ──
            if diag['zone_ok']:
                if choch_existe:
                    diag_agregado['combinations']['zone_ok_choch_exists'] += 1
                else:
                    diag_agregado['combinations']['zone_ok_choch_absent'] += 1
            else:
                if choch_existe:
                    diag_agregado['combinations']['zone_fail_choch_exists'] += 1
                else:
                    diag_agregado['combinations']['zone_fail_choch_absent'] += 1

            if choch_existe:
                if fvg_existe:
                    diag_agregado['combinations']['choch_exists_fvg_exists'] += 1
                else:
                    diag_agregado['combinations']['choch_exists_fvg_absent'] += 1

        if r_legacy.get('fvg_candidatos'):
            legacy_signals += 1
        if r_vortex.get('fvg_candidatos'):
            luxalgo_valid_setups += 1
        if r_vortex.get('motivo_rejeicao') not in ('FVG_CHOCH_RELATION_UNKNOWN',) and r_vortex.get('fvg_candidatos'):
            vortex_decisions_nao_unknown += 1

        motivo_legacy = r_legacy.get('motivo_rejeicao') or 'NENHUM'
        motivo_luxalgo = r_vortex.get('motivo_rejeicao') or 'NENHUM'
        rejection_reasons_legacy[motivo_legacy] = rejection_reasons_legacy.get(motivo_legacy, 0) + 1
        rejection_reasons_luxalgo[motivo_luxalgo] = rejection_reasons_luxalgo.get(motivo_luxalgo, 0) + 1

        # ── comparações campo a campo, guardando timestamp de cada divergência ──
        bias_legacy, bias_lux = r_legacy.get('bias'), r_vortex.get('bias')
        if bias_legacy != bias_lux:
            bias_divergences += 1
            if len(eventos_divergentes) < MAX_EVENTOS_DIVERGENTES_GUARDADOS:
                eventos_divergentes.append({'t': ts_corte, 'campo': 'bias', 'legacy': bias_legacy, 'luxalgo': bias_lux})

        zona_legacy, zona_lux = r_legacy.get('zona'), r_vortex.get('zona')
        if zona_legacy != zona_lux:
            zone_divergences += 1
            if len(eventos_divergentes) < MAX_EVENTOS_DIVERGENTES_GUARDADOS:
                eventos_divergentes.append({'t': ts_corte, 'campo': 'zone', 'legacy': zona_legacy, 'luxalgo': zona_lux})

        choch_legacy_existe = r_legacy.get('choch') is not None
        choch_lux_existe = r_vortex.get('internal_choch') is not None
        if choch_legacy_existe != choch_lux_existe:
            choch_divergences += 1
            if len(eventos_divergentes) < MAX_EVENTOS_DIVERGENTES_GUARDADOS:
                eventos_divergentes.append({
                    't': ts_corte, 'campo': 'choch',
                    'legacy': r_legacy.get('choch'), 'luxalgo': r_vortex.get('internal_choch'),
                })

        fvg_legacy_n = len(r_legacy.get('fvg_candidatos') or [])
        fvg_lux_n = len(r_vortex.get('fvg_candidatos') or [])
        if fvg_legacy_n != fvg_lux_n:
            fvg_divergences += 1
            if len(eventos_divergentes) < MAX_EVENTOS_DIVERGENTES_GUARDADOS:
                eventos_divergentes.append({'t': ts_corte, 'campo': 'fvg_count', 'legacy': fvg_legacy_n, 'luxalgo': fvg_lux_n})

    return {
        'pair': pair, 'dias_historico': dias_historico,
        'nota_metodologica': (
            'Replay SOMENTE LEITURA, sem escrita em produção, sem chamar '
            'process_pair_gates_vortex() nem process_pair_4camadas(), sem lookahead '
            '(cada ciclo só enxerga m5[:i+1], mesmo mecanismo causal já validado em '
            'replay_gates_reprovados). LEGACY usa compute_lux_structure_bias (já '
            'existente antes deste ticket) + compute_premium_discount (zona fixa 20 '
            'candles) + detect_sweep_in_zone/detect_choch_after_sweep (SWEEP_BASED) + '
            'find_open_fvgs (threshold fixo). LUXALGO/VORTEX usa compute_lux_structure_bias '
            '(mesma função — por isso bias_divergences deve ficar em 0, não é bug) + '
            'compute_lux_premium_discount (trailing dinâmico) + compute_lux_internal_structure '
            '(LUX_INTERNAL_CHoCH) + find_open_fvgs_adaptive (threshold adaptativo). Nenhum dos '
            'dois caminhos gera entry/sl/tp — ambos são scaffolds de auditoria estrutural.'
        ),
        'resumo': {
            'total_candles': total_candles,
            'legacy_signals': legacy_signals,
            'luxalgo_valid_setups': luxalgo_valid_setups,
            'vortex_decisions_com_fvg_candidato': vortex_decisions_nao_unknown,
            'bias_divergences': bias_divergences,
            'zone_divergences': zone_divergences,
            'choch_divergences': choch_divergences,
            'fvg_divergences': fvg_divergences,
        },
        'rejection_reasons': {
            'legacy': rejection_reasons_legacy,
            'luxalgo': rejection_reasons_luxalgo,
        },
        'eventos_divergentes': eventos_divergentes,
        'eventos_divergentes_truncado': len(eventos_divergentes) >= MAX_EVENTOS_DIVERGENTES_GUARDADOS,
        'diagnostico_independente': diag_agregado,
        'medicao_idade_choch': {
            'nota': (
                'Item aprovado do ticket — mede a idade real (em candles e minutos) do CHoCH '
                'que a implementação atual efetivamente seleciona, SEM aplicar nenhuma janela '
                'de recência. Objetivo: medir antes de decidir se e qual janela faz sentido.'
            ),
            'estatisticas': _agregar_medicao_idade_choch(medicoes_idade_choch),
            'validacao_correspondencia': {
                'nota': (
                    'Confirma que o CHoCH medido aqui é exatamente o mesmo que diagnostico_'
                    'independente_luxalgo() já reportou em internal_choch_evento (mesmo '
                    'timestamp e index) — cross-check de que estamos medindo o evento certo, '
                    'não outro CHoCH qualquer.'
                ),
                'ok': validacao_correspondencia_choch['ok'],
                'divergente': validacao_correspondencia_choch['divergente'],
            },
            'amostra_bruta_primeiros_50': medicoes_idade_choch[:50],
            'amostra_bruta_ultimos_50': medicoes_idade_choch[-50:] if len(medicoes_idade_choch) > 50 else [],
            'total_medicoes_brutas': len(medicoes_idade_choch),
        },
    }


@explicacao_bp.route("/scalp_gates_vortex/replay_comparativo_luxalgo", methods=["GET"])
def replay_comparativo_luxalgo_endpoint():
    """
    Item E do ticket — endpoint do replay comparativo LEGACY vs
    LUXALGO vs VORTEX_LAYER. Protegido contra chamada acidental,
    mesmo padrão dos outros endpoints de replay.
    Uso: ?pair=ADAUSD&dias=7&confirm=RODAR_REPLAY_COMPARATIVO
    """
    if request.args.get('confirm') != 'RODAR_REPLAY_COMPARATIVO':
        return jsonify({
            "erro": "endpoint pesado, protegido contra chamada acidental",
            "como_usar": "adiciona &confirm=RODAR_REPLAY_COMPARATIVO na URL, ex: "
                          "/scalp_gates_vortex/replay_comparativo_luxalgo?pair=ADAUSD&dias=7&confirm=RODAR_REPLAY_COMPARATIVO",
        }), 400
    pair = request.args.get('pair', 'BTCUSD')
    dias = int(request.args.get('dias', 7))
    try:
        report = replay_comparativo_luxalgo(pair, dias_historico=dias)
    except Exception as e:
        return jsonify({"erro": str(e)}), 500
    return jsonify(report)


@explicacao_bp.route("/scalp_gates_vortex/replay_gates_reprovados", methods=["GET"])
def replay_gates_reprovados_endpoint():
    """
    Fase 1 do shadow/replay de oportunidades recusadas — categoria
    gates_reprovados apenas. Ferramenta de ANALISE, fora do pipeline de
    producao. Pesado (varre candle a candle todo o historico), protegido
    contra chamada acidental.
    Uso: ?pair=AAVEUSD&dias=30&confirm=RODAR_REPLAY
    Uso resumido (sem os candles/MSS embutidos em cada avaliacao bruta,
    so os campos agregados: funil, gates_detalhe sem avaliacoes_brutas,
    simulacao_cenarios_gates): acrescenta &resumo=true
    """
    if request.args.get('confirm') != 'RODAR_REPLAY':
        return jsonify({
            "erro": "endpoint pesado, protegido contra chamada acidental",
            "como_usar": "adiciona &confirm=RODAR_REPLAY na URL, ex: "
                          "/scalp_gates_vortex/replay_gates_reprovados?pair=AAVEUSD&dias=30&confirm=RODAR_REPLAY",
        }), 400
    pair = request.args.get('pair', 'BTCUSD')
    dias = int(request.args.get('dias', 30))
    resumo = request.args.get('resumo', 'false').lower() == 'true'
    try:
        report = replay_gates_reprovados(pair, dias_historico=dias)
    except Exception as e:
        return jsonify({"erro": str(e)}), 500

    if resumo and 'erro' not in report:
        # Filtro puramente de apresentacao - remove so os campos
        # volumosos (candles/MSS embutidos em cada avaliacao bruta),
        # sem alterar nenhum calculo ja feito.
        report = dict(report)
        if 'gates_detalhe' in report:
            gd = dict(report['gates_detalhe'])
            gd.pop('avaliacoes_brutas', None)
            report['gates_detalhe'] = gd
        if 'identidade_temporal_eventos' in report:
            ite = dict(report['identidade_temporal_eventos'])
            avals_enxutas = []
            for av in ite.get('avaliacoes', []):
                av_enxuta = {k: v for k, v in av.items() if k not in ('gate_a_operandos', 'gate_b_operandos', 'gate_c_operandos')}
                for chave_gate in ('gate_a_operandos', 'gate_b_operandos', 'gate_c_operandos'):
                    dados_gate = av.get(chave_gate)
                    if dados_gate:
                        dg = {k: v for k, v in dados_gate.items() if k not in ('mss', 'entry_zone')}
                        av_enxuta[chave_gate] = dg
                avals_enxutas.append(av_enxuta)
            ite['avaliacoes'] = avals_enxutas
            report['identidade_temporal_eventos'] = ite
        if 'clusters_detalhe' in report:
            cd = dict(report['clusters_detalhe'])
            clusters_enxutos = []
            for c in cd.get('clusters', []):
                c_enxuto = {k: v for k, v in c.items() if k != 'gates_reprovados_por_avaliacao'}
                clusters_enxutos.append(c_enxuto)
            cd['clusters'] = clusters_enxutos
            report['clusters_detalhe'] = cd
        if 'simulacao_cenarios_gates' in report:
            sc = dict(report['simulacao_cenarios_gates'])
            cenarios_enxutos = {}
            for nome_cenario, dados_cenario in sc.get('cenarios', {}).items():
                dc = {k: v for k, v in dados_cenario.items() if k != 'detalhe_candidatos'}
                dc['n_candidatos_detalhe_omitido'] = len(dados_cenario.get('detalhe_candidatos', []))
                cenarios_enxutos[nome_cenario] = dc
            sc['cenarios'] = cenarios_enxutos
            report['simulacao_cenarios_gates'] = sc

    return jsonify(report)


PARES_MONITORADOS_REPLAY = [
    'BTCUSD', 'ETHUSD', 'SOLUSD', 'XRPUSD', 'LINKUSD', 'ADAUSD',
    'AVAXUSD', 'BNBUSD', 'AAVEUSD', 'NEARUSD', 'PENDLEUSD', 'INJUSD', 'ONDOUSD',
]


def replay_gates_reprovados_todos_pares(dias_historico=7, pares=None):
    """
    Roda replay_gates_reprovados() (SEM ALTERAÇÃO NENHUMA) em sequência
    para cada par da lista, e agrega os resultados. Cada par é 100%
    independente — erro num par não derruba os demais (fica registrado
    em 'erro' dentro do resultado daquele par específico).

    Mesma metodologia, mesmas garantias de segurança de
    replay_gates_reprovados(): db_file temporário/descartável por
    chamada, sem lookahead, sem tocar produção, sem Telegram, sem sinal
    real. Esta função só ORQUESTRA múltiplas chamadas — não duplica
    nenhuma lógica de replay nem de produção.
    """
    pares = pares or PARES_MONITORADOS_REPLAY
    resultados_por_pair = {}

    total_brutas = 0
    total_unicas = 0
    total_long = 0
    total_short = 0
    pares_com_erro = []
    pares_com_dados_insuficientes = []

    for p in pares:
        try:
            r = replay_gates_reprovados(p, dias_historico=dias_historico)
        except Exception as e:
            resultados_por_pair[p] = {'erro': str(e)}
            pares_com_erro.append(p)
            continue

        resultados_por_pair[p] = r

        if 'erro' in r:
            pares_com_dados_insuficientes.append(p)
            continue

        total_brutas += r.get('oportunidades_brutas_antes_dedup', 0) or 0
        total_unicas += r.get('oportunidades_unicas_apos_dedup', 0) or 0
        total_long += r.get('oportunidades_long', 0) or 0
        total_short += r.get('oportunidades_short', 0) or 0

    # ── Agregação do FUNIL completo, somando todos os pares — item 3/4
    # do ticket. Cada campo é soma direta dos contadores por par (só
    # leitura, nenhuma lógica de decisão nova). ──
    funil_agregado = {}
    violacoes_causais_h1_total = 0
    for p, r in resultados_por_pair.items():
        if 'erro' in r:
            continue
        for chave, valor in (r.get('funil') or {}).items():
            funil_agregado[chave] = funil_agregado.get(chave, 0) + (valor or 0)
        violacoes_causais_h1_total += (r.get('validacao_causal_h1') or {}).get('violacoes_causais_h1', 0) or 0

    # ── Agregação combinada por horizonte, juntando as oportunidades de
    # TODOS os pares que tiveram dados válidos — para ter uma amostra
    # estatisticamente mais robusta que par por par isolado. ──
    agregado_por_horizonte = {}
    for h in GATES_REPROVADOS_HORIZONTES_CANDLES:
        chave_h = f'{h}_candles_M5'
        n_tp1 = n_tp2 = n_sl = n_amb = n_nen = 0
        for p, r in resultados_por_pair.items():
            if 'erro' in r:
                continue
            bloco = r.get('por_horizonte', {}).get(chave_h, {})
            n_tp1 += bloco.get('TP1', 0) or 0
            n_tp2 += bloco.get('TP2', 0) or 0
            n_sl += bloco.get('SL', 0) or 0
            n_amb += bloco.get('AMBIGUO', 0) or 0
            n_nen += bloco.get('NENHUM', 0) or 0

        n_amostra = n_tp1 + n_tp2 + n_sl + n_amb + n_nen
        n_win = n_tp1 + n_tp2
        n_resolvidos = n_win + n_sl
        win_rate = round(100 * n_win / n_resolvidos, 1) if n_resolvidos else None

        agregado_por_horizonte[chave_h] = {
            'amostra_total_todos_pares': n_amostra,
            'amostra_suficiente': n_amostra >= 30,
            'nota': None if n_amostra >= 30 else 'AMOSTRA INSUFICIENTE PARA CONCLUSÃO',
            'TP1': n_tp1, 'TP2': n_tp2, 'SL': n_sl, 'AMBIGUO': n_amb, 'NENHUM': n_nen,
            'win_rate_pct': win_rate,
            'loss_rate_pct': round(100 - win_rate, 1) if win_rate is not None else None,
        }

    # ── Agregação da SIMULAÇÃO DE CENÁRIOS (item aprovado do ticket) —
    # soma entradas_simuladas/TP1/TP2/SL/AMBIGUO/NENHUM de cada cenário
    # através de TODOS os pares, e recalcula win_rate/expectancy sobre a
    # amostra agregada (maior que qualquer par isolado). Nenhum gate é
    # recalculado — só somamos o que replay_gates_reprovados() já
    # calculou por par. ──
    # ── Agregado do diagnóstico SFP (Casos A-E) por par e global ──
    diagnostico_sfp_por_pair = {}
    diagnostico_sfp_global = {}
    for p, r in resultados_por_pair.items():
        if 'erro' in r:
            continue
        contagem = (r.get('diagnostico_sfp_casos') or {}).get('contagem_por_caso') or {}
        diagnostico_sfp_por_pair[p] = contagem
        for caso, n in contagem.items():
            diagnostico_sfp_global[caso] = diagnostico_sfp_global.get(caso, 0) + n

    cenarios_agregados = {c: {
        'entradas_simuladas_total': 0, 'TP1': 0, 'TP2': 0, 'SL': 0, 'AMBIGUO': 0, 'NENHUM': 0,
        'mfe_soma': 0.0, 'mae_soma': 0.0, 'rr_soma': 0.0, 'rr_n': 0,
        'pares_com_entrada': [],
    } for c in CENARIOS_SIMULACAO_GATES}

    for p, r in resultados_por_pair.items():
        if 'erro' in r:
            continue
        cenarios_par = (r.get('simulacao_cenarios_gates') or {}).get('cenarios', {})
        for nome_cenario, dc in cenarios_par.items():
            if nome_cenario not in cenarios_agregados:
                continue
            ca = cenarios_agregados[nome_cenario]
            n_entradas = dc.get('entradas_simuladas', 0) or 0
            ca['entradas_simuladas_total'] += n_entradas
            ca['TP1'] += dc.get('TP1', 0) or 0
            ca['TP2'] += dc.get('TP2', 0) or 0
            ca['SL'] += dc.get('SL', 0) or 0
            ca['AMBIGUO'] += dc.get('AMBIGUO', 0) or 0
            ca['NENHUM'] += dc.get('NENHUM', 0) or 0
            if n_entradas > 0:
                ca['pares_com_entrada'].append(p)
            if dc.get('mfe_medio_pct') is not None:
                ca['mfe_soma'] += dc['mfe_medio_pct'] * n_entradas
            if dc.get('mae_medio_pct') is not None:
                ca['mae_soma'] += dc['mae_medio_pct'] * n_entradas
            if dc.get('rr_medio_realizado') is not None:
                ca['rr_soma'] += dc['rr_medio_realizado']
                ca['rr_n'] += 1

    cenarios_agregados_final = {}
    for nome_cenario, ca in cenarios_agregados.items():
        n_win = ca['TP1'] + ca['TP2']
        n_resolvidos = n_win + ca['SL']
        win_rate = round(100 * n_win / n_resolvidos, 1) if n_resolvidos else None
        rr_medio = round(ca['rr_soma'] / ca['rr_n'], 2) if ca['rr_n'] else None
        expectancy = None
        if win_rate is not None and rr_medio is not None:
            wr = win_rate / 100
            expectancy = round((wr * rr_medio) - (1 - wr), 4)
        cenarios_agregados_final[nome_cenario] = {
            'entradas_simuladas_total': ca['entradas_simuladas_total'],
            'pares_com_pelo_menos_1_entrada': ca['pares_com_entrada'],
            'TP1': ca['TP1'], 'TP2': ca['TP2'], 'SL': ca['SL'],
            'AMBIGUO': ca['AMBIGUO'], 'NENHUM': ca['NENHUM'],
            'win_rate_pct': win_rate,
            'mfe_medio_pct_ponderado': round(ca['mfe_soma'] / ca['entradas_simuladas_total'], 4) if ca['entradas_simuladas_total'] else None,
            'mae_medio_pct_ponderado': round(ca['mae_soma'] / ca['entradas_simuladas_total'], 4) if ca['entradas_simuladas_total'] else None,
            'rr_medio_realizado': rr_medio,
            'expectancy': expectancy,
            'amostra_suficiente': n_resolvidos >= 30,
            'nota': None if n_resolvidos >= 30 else 'AMOSTRA INSUFICIENTE PARA CONCLUSÃO',
        }

    return {
        'dias_historico': dias_historico,
        'pares_testados': pares,
        'pares_com_erro_ou_dados_insuficientes': pares_com_dados_insuficientes + pares_com_erro,
        'resumo_geral': {
            'oportunidades_brutas_antes_dedup_total': total_brutas,
            'oportunidades_unicas_apos_dedup_total': total_unicas,
            'oportunidades_long_total': total_long,
            'oportunidades_short_total': total_short,
        },
        'validacao_causal_h1_todos_pares': {
            'violacoes_causais_h1_total': violacoes_causais_h1_total,
            'h1_100_por_cento_causal_em_todos_os_pares': violacoes_causais_h1_total == 0,
        },
        'funil_agregado_todos_pares': funil_agregado,
        'agregado_por_horizonte_todos_pares': agregado_por_horizonte,
        'diagnostico_sfp_casos_por_pair': diagnostico_sfp_por_pair,
        'diagnostico_sfp_casos_global': diagnostico_sfp_global,
        'simulacao_cenarios_agregado_todos_pares': cenarios_agregados_final,
        'nota_metodologica': (
            'Cada par foi processado de forma totalmente independente, chamando '
            'replay_gates_reprovados() sem nenhuma alteração — mesma metodologia, mesmas '
            'garantias (db_file temporário por chamada, sem lookahead, sem tocar produção). '
            'O agregado_por_horizonte_todos_pares soma os resultados de todos os pares com '
            'dados válidos, para dar uma amostra maior. Detalhe completo de cada par está em '
            '"resultados_por_pair".'
        ),
        'resultados_por_pair': resultados_por_pair,
    }


@explicacao_bp.route("/scalp_gates_vortex/replay_gates_reprovados_todos_pares", methods=["GET"])
def replay_gates_reprovados_todos_pares_endpoint():
    """
    Roda a Fase 1 do shadow/replay (gates_reprovados) em TODOS os pares
    monitorados de uma vez, e devolve o agregado + detalhe por par.
    MUITO pesado - roda o replay completo (candle a candle) pra cada um
    dos ~13 pares em sequencia. Pode demorar bastante e arriscar timeout
    dependendo do limite do servidor. Protegido contra chamada acidental.
    Uso: ?dias=7&confirm=RODAR_REPLAY_TODOS
    Uso resumido (sem os detalhes por avaliacao/cluster de cada par, so
    os agregados: funil_agregado_todos_pares, simulacao_cenarios_agregado_
    todos_pares, e um resumo minimo por par): acrescenta &resumo=true
    Uso com subconjunto de pares (para testar antes de rodar todos os 13,
    ou dividir em lotes menores): acrescenta &pares=BTCUSD,ETHUSD,LINKUSD
    (sem espaco, separado por virgula). Sem esse parametro, roda os 13
    pares padrao (PARES_MONITORADOS_REPLAY).
    """
    if request.args.get('confirm') != 'RODAR_REPLAY_TODOS':
        return jsonify({
            "erro": "endpoint MUITO pesado (roda todos os pares em sequencia), protegido contra chamada acidental",
            "como_usar": "adiciona &confirm=RODAR_REPLAY_TODOS na URL, ex: "
                          "/scalp_gates_vortex/replay_gates_reprovados_todos_pares?dias=7&confirm=RODAR_REPLAY_TODOS",
            "aviso": "pode demorar varios minutos - considera rodar com dias baixo (ex: 7) primeiro",
        }), 400
    dias = int(request.args.get('dias', 7))
    resumo = request.args.get('resumo', 'false').lower() == 'true'
    pares_param = request.args.get('pares')
    pares_lista = None
    if pares_param:
        pares_lista = [p.strip().upper() for p in pares_param.split(',') if p.strip()]
    try:
        report = replay_gates_reprovados_todos_pares(dias_historico=dias, pares=pares_lista)
    except Exception as e:
        return jsonify({"erro": str(e)}), 500

    if resumo:
        # Filtro puramente de apresentacao - resultados_por_pair vira um
        # resumo minimo por par (funil + gates_detalhe sem avaliacoes +
        # simulacao_cenarios_gates sem detalhe_candidatos), sem alterar
        # nenhum calculo ja feito. Os agregados globais ficam completos.
        report = dict(report)
        resultados_por_pair_resumido = {}
        for p, r in (report.get('resultados_por_pair') or {}).items():
            if 'erro' in r:
                resultados_por_pair_resumido[p] = r
                continue
            r_resumido = {
                'pair': r.get('pair'),
                'dados_historicos': r.get('dados_historicos'),
                'funil': r.get('funil'),
                'diagnostico_sfp_casos': (r.get('diagnostico_sfp_casos') or {}).get('contagem_por_caso'),
                'correlacao_diag_vs_decisao_real': (r.get('diagnostico_sfp_casos') or {}).get('correlacao_diag_vs_decisao_real'),
                'motivos_nao_classificados_texto_exato': (r.get('diagnostico_sfp_casos') or {}).get('motivos_nao_classificados_texto_exato'),
                'oportunidades_brutas_antes_dedup': r.get('oportunidades_brutas_antes_dedup'),
                'clusters_por_identidade_real_preliminar': r.get('clusters_por_identidade_real_preliminar'),
                'validacao_causal_h1': {
                    'violacoes_causais_h1': (r.get('validacao_causal_h1') or {}).get('violacoes_causais_h1'),
                    'h1_100_por_cento_causal': (r.get('validacao_causal_h1') or {}).get('h1_100_por_cento_causal'),
                },
            }
            gd = r.get('gates_detalhe') or {}
            r_resumido['gates_detalhe_resumo'] = {
                'agregado_por_gate': gd.get('agregado_por_gate'),
                'combinacao_mais_frequente_reprovacao': gd.get('combinacao_mais_frequente_reprovacao'),
                'operandos_agregado_por_subcondicao': gd.get('operandos_agregado_por_subcondicao'),
            }
            sc = r.get('simulacao_cenarios_gates') or {}
            cenarios_resumidos = {}
            for nome_c, dc in (sc.get('cenarios') or {}).items():
                cenarios_resumidos[nome_c] = {k: v for k, v in dc.items() if k != 'detalhe_candidatos'}
            r_resumido['simulacao_cenarios_gates_resumo'] = cenarios_resumidos
            resultados_por_pair_resumido[p] = r_resumido
        report['resultados_por_pair'] = resultados_por_pair_resumido

    return jsonify(report)


@explicacao_bp.route("/scalp_gates_vortex/replay_liquidez", methods=["GET"])
def replay_liquidez_endpoint():
    """
    Ferramenta de ANÁLISE/RESEARCH, fora do pipeline de produção. Busca
    histórico real da Bybit (~centenas de candles por par) e compara 4
    referências de liquidez lado a lado — é pesado, então exige
    confirmação explícita pra evitar chamada acidental/automática.
    Uso: ?pair=BTCUSD&dias=30&confirm=RODAR_REPLAY
    Demora alguns segundos (várias chamadas paginadas à API da Bybit).
    """
    if request.args.get('confirm') != 'RODAR_REPLAY':
        return jsonify({
            "erro": "endpoint pesado, protegido contra chamada acidental",
            "como_usar": "adiciona &confirm=RODAR_REPLAY na URL, ex: "
                          "/scalp_gates_vortex/replay_liquidez?pair=BTCUSD&dias=30&confirm=RODAR_REPLAY",
        }), 400
    pair = request.args.get('pair', 'BTCUSD')
    dias = int(request.args.get('dias', 30))
    try:
        report = replay_comparar_referencias_liquidez(pair, dias_historico=dias)
    except Exception as e:
        return jsonify({"erro": str(e)}), 500
    return jsonify(report)


# ═══════════════════════════════════════════════════════════════════════
# REPLAY EM BACKGROUND — resolve o timeout de conexão HTTP do
# replay_comparativo_luxalgo (pesado demais pra terminar dentro do
# limite de uma requisição). NÃO altera replay_comparativo_luxalgo()
# nem nenhuma outra função existente — só adiciona uma camada de
# orquestração por cima: inicia a mesma função em background numa
# thread, persiste o resultado numa tabela isolada (não é tabela de
# decisão/produção, é só armazenamento de resultado de diagnóstico,
# mesmo padrão já usado por scalp_gates_vortex_sfp_diagnostico etc.),
# e devolve um job_id na hora, sem esperar o replay terminar.
# ═══════════════════════════════════════════════════════════════════════

def init_replay_jobs_db(db_file):
    """Cria a tabela de jobs de replay em background, se não existir.
    Auto-blindado — não depende de init no boot do app.py."""
    try:
        with sqlite3.connect(db_file) as conn:
            conn.execute('''
                CREATE TABLE IF NOT EXISTS scalp_replay_jobs (
                    job_id TEXT PRIMARY KEY,
                    tipo TEXT,
                    pair TEXT,
                    dias_historico INTEGER,
                    status TEXT DEFAULT 'rodando',
                    resultado_json TEXT,
                    erro TEXT,
                    created_at INTEGER,
                    finished_at INTEGER
                )
            ''')
            conn.commit()
    except Exception as e:
        print(f"[scalp_engine replay_jobs] erro ao criar tabela: {e}")


def _executar_replay_comparativo_job(db_file, job_id, pair, dias_historico):
    """
    Roda EXATAMENTE replay_comparativo_luxalgo() (função existente, sem
    nenhuma alteração), só que dentro de uma thread separada — a
    requisição HTTP que disparou isso já respondeu há muito tempo, essa
    função roda sozinha em background até terminar (sem prazo de
    navegador/timeout de conexão). Ao final, grava o resultado (ou o
    erro) na tabela scalp_replay_jobs. Fail-safe: qualquer exceção aqui
    é capturada e registrada como status='erro', nunca derruba o
    processo do servidor.
    """
    try:
        resultado = replay_comparativo_luxalgo(pair, dias_historico=dias_historico)
        status_final = 'erro' if (isinstance(resultado, dict) and 'erro' in resultado) else 'concluido'
        with sqlite3.connect(db_file) as conn:
            conn.execute('''
                UPDATE scalp_replay_jobs
                SET status=?, resultado_json=?, finished_at=?
                WHERE job_id=?
            ''', (status_final, json.dumps(resultado, ensure_ascii=False), int(time.time()), job_id))
            conn.commit()
    except Exception as e:
        try:
            with sqlite3.connect(db_file) as conn:
                conn.execute('''
                    UPDATE scalp_replay_jobs
                    SET status='erro', erro=?, finished_at=?
                    WHERE job_id=?
                ''', (str(e), int(time.time()), job_id))
                conn.commit()
        except Exception as e2:
            print(f"[scalp_engine replay_jobs] erro ao registrar falha do job {job_id}: {e2}")


@explicacao_bp.route("/scalp_gates_vortex/replay_comparativo_luxalgo_iniciar", methods=["GET"])
def replay_comparativo_luxalgo_iniciar_endpoint():
    """
    Inicia replay_comparativo_luxalgo() em BACKGROUND (thread separada)
    e devolve um job_id IMEDIATAMENTE, sem esperar o replay terminar —
    resolve o ERR_CONNECTION_CLOSED que acontece quando o replay é
    pesado demais pra terminar dentro do tempo de uma requisição HTTP.
    NÃO altera replay_comparativo_luxalgo() nem nenhuma função de
    decisão/produção — só orquestra a mesma função existente por cima.
    Uso: ?pair=ADAUSD&dias=7&confirm=RODAR_REPLAY_COMPARATIVO
    Depois de iniciar, consulta o resultado em:
    /scalp_gates_vortex/replay_comparativo_luxalgo_status/<job_id>
    """
    if request.args.get('confirm') != 'RODAR_REPLAY_COMPARATIVO':
        return jsonify({
            "erro": "endpoint pesado, protegido contra chamada acidental",
            "como_usar": "adiciona &confirm=RODAR_REPLAY_COMPARATIVO na URL, ex: "
                          "/scalp_gates_vortex/replay_comparativo_luxalgo_iniciar?pair=ADAUSD&dias=7&confirm=RODAR_REPLAY_COMPARATIVO",
        }), 400

    pair = request.args.get('pair', 'BTCUSD')
    dias = int(request.args.get('dias', 7))
    db_file = _db_file_explicacao()
    init_replay_jobs_db(db_file)

    job_id = f"replaycomp_{pair}_{int(time.time()*1000)}"

    try:
        with sqlite3.connect(db_file) as conn:
            conn.execute('''
                INSERT INTO scalp_replay_jobs (job_id, tipo, pair, dias_historico, status, created_at)
                VALUES (?, ?, ?, ?, 'rodando', ?)
            ''', (job_id, 'replay_comparativo_luxalgo', pair, dias, int(time.time())))
            conn.commit()
    except Exception as e:
        return jsonify({"erro": f"não foi possível criar o job: {e}"}), 500

    thread = threading.Thread(
        target=_executar_replay_comparativo_job,
        args=(db_file, job_id, pair, dias),
        daemon=True,
    )
    thread.start()

    return jsonify({
        "job_id": job_id,
        "status": "iniciado",
        "pair": pair,
        "dias_historico": dias,
        "como_consultar": f"/scalp_gates_vortex/replay_comparativo_luxalgo_status/{job_id}",
        "aviso": "o replay roda em background — pode levar alguns minutos, consulte o job_id acima quando quiser",
    })


@explicacao_bp.route("/scalp_gates_vortex/replay_comparativo_luxalgo_status/<job_id>", methods=["GET"])
def replay_comparativo_luxalgo_status_endpoint(job_id):
    """
    Consulta o status/resultado de um job iniciado via
    /scalp_gates_vortex/replay_comparativo_luxalgo_iniciar. Devolve
    status='rodando' (ainda não terminou), 'concluido' (com o resultado
    completo em 'resultado') ou 'erro' (com a mensagem em 'erro').
    Só leitura — não recalcula nada, não altera nenhum job.
    """
    db_file = _db_file_explicacao()
    init_replay_jobs_db(db_file)
    try:
        with sqlite3.connect(db_file) as conn:
            row = conn.execute('''
                SELECT status, resultado_json, erro, pair, dias_historico, created_at, finished_at
                FROM scalp_replay_jobs WHERE job_id=?
            ''', (job_id,)).fetchone()
    except Exception as e:
        return jsonify({"erro": str(e)}), 500

    if not row:
        return jsonify({"erro": f"job_id não encontrado: {job_id}"}), 404

    status, resultado_json, erro, pair, dias, created_at, finished_at = row
    resposta = {
        "job_id": job_id, "status": status, "pair": pair, "dias_historico": dias,
        "created_at": created_at, "finished_at": finished_at,
    }
    if status == 'concluido' and resultado_json:
        resposta["resultado"] = json.loads(resultado_json)
    elif status == 'erro':
        resposta["erro"] = erro
    else:
        segundos_rodando = int(time.time()) - created_at
        resposta["segundos_rodando"] = segundos_rodando
        resposta["nota"] = "ainda processando — tenta consultar de novo em alguns segundos/minutos"

    return jsonify(resposta)


# ═══════════════════════════════════════════════════════════════════════
# MEDIÇÃO DE RECÊNCIA DO CHoCH — item aprovado do ticket. SOMENTE
# MEDIÇÃO, não altera nenhuma regra de decisão, não define janela
# nenhuma. Objetivo: descobrir a idade real (em candles/minutos) do
# CHoCH que a implementação atual está de fato usando quando diz
# internal_choch_exists=True, ANTES de decidir se precisa de janela de
# recência e qual valor usar.
# ═══════════════════════════════════════════════════════════════════════

def _selecionar_choch_para_medicao(candles_internal, bias, idx_atual):
    """
    Reproduz, pra fins de MEDIÇÃO apenas, a MESMA seleção de CHoCH já
    usada em diagnostico_independente_luxalgo()/avaliar_vortex_
    decision_layer() — "for ev in reversed(eventos_internos): if
    tipo==CHoCH e direcao==bias: usa esse, para" — sem alterar nenhuma
    dessas funções existentes. Reaproveita compute_lux_internal_
    structure() (não duplica detecção nenhuma, só a seleção/medição).
    Retorna None se não houver CHoCH na direção do bias (mesmo critério
    de ausência já usado).
    """
    if bias not in ('alta', 'baixa'):
        return None
    eventos_internos = compute_lux_internal_structure(candles_internal, swing_size=5)
    choch_relevante = None
    for ev in reversed(eventos_internos):
        if ev['tipo'] == 'CHoCH' and ev['direcao'] == bias:
            choch_relevante = ev
            break
    if not choch_relevante:
        return None

    candle_atual = candles_internal[idx_atual]
    idade_em_candles = idx_atual - choch_relevante['index']
    idade_em_minutos = round((candle_atual['t'] - choch_relevante['t']) / 60000, 2)

    return {
        'candle_atual_ts': candle_atual['t'],
        'choch_ts': choch_relevante['t'],
        'choch_index': choch_relevante['index'],
        'idx_atual': idx_atual,
        'idade_em_candles': idade_em_candles,
        'idade_em_minutos': idade_em_minutos,
        'direcao_bias': bias,
        'direcao_choch': choch_relevante['direcao'],
    }


def _agregar_medicao_idade_choch(lista_medicoes):
    """
    Agrega as estatísticas obrigatórias do ticket — puramente
    aritmético sobre a lista de medições já coletadas, não recalcula
    nenhuma detecção. Distribuição em buckets fixos pedidos:
    0-5, 6-10, 11-20, 21-50, 51-100, 101-200, 201-500, 501+.
    """
    if not lista_medicoes:
        return {
            'total_choch_detectados': 0,
            'nota': 'nenhum CHoCH detectado na amostra — sem estatística possível',
        }

    idades = sorted(m['idade_em_candles'] for m in lista_medicoes)
    n = len(idades)

    def media(lista):
        return round(sum(lista) / len(lista), 2) if lista else None

    buckets_definicao = [
        ('0-5', 0, 5), ('6-10', 6, 10), ('11-20', 11, 20), ('21-50', 21, 50),
        ('51-100', 51, 100), ('101-200', 101, 200), ('201-500', 201, 500),
        ('501+', 501, float('inf')),
    ]
    distribuicao = {label: 0 for label, _, _ in buckets_definicao}
    for idade in idades:
        for label, minimo, maximo in buckets_definicao:
            if minimo <= idade <= maximo:
                distribuicao[label] += 1
                break

    return {
        'total_choch_detectados': n,
        'idade_min': idades[0],
        'idade_max': idades[-1],
        'idade_media': media(idades),
        'idade_mediana': _percentil(idades, 50),
        'percentil_25': _percentil(idades, 25),
        'percentil_50': _percentil(idades, 50),
        'percentil_75': _percentil(idades, 75),
        'percentil_90': _percentil(idades, 90),
        'percentil_95': _percentil(idades, 95),
        'percentil_99': _percentil(idades, 99),
        'distribuicao_por_bucket_candles': distribuicao,
        'nota': (
            'idade_em_candles = idx_atual - choch_index, medido no mesmo array de candles '
            'passado pra compute_lux_internal_structure() em cada ciclo (candles_ate_agora, '
            'sem lookahead). NENHUMA janela de recência foi aplicada aqui — esta é a idade '
            'REAL do CHoCH que a implementação atual efetivamente usa, sem filtro nenhum.'
        ),
    }


# ═══════════════════════════════════════════════════════════════════════
# SIMULAÇÃO COMPARATIVA DE RECÊNCIA DO CHoCH — item aprovado do ticket.
# SOMENTE REPLAY/SIMULAÇÃO, não altera nenhuma regra de decisão real.
# Testa hipoteticamente 4 janelas de recência (50/100/150/200 candles)
# lado a lado com o comportamento CURRENT (sem limite, já medido),
# reaproveitando exatamente as mesmas funções já existentes
# (compute_lux_structure_bias, compute_lux_premium_discount,
# classificar_zona_lux, compute_lux_internal_structure,
# find_open_fvgs_adaptive) — nenhuma delas é alterada. NÃO chama
# avaliar_vortex_decision_layer() nem process_pair_gates_vortex() nem
# process_pair_4camadas(). Não decide nada de produção, não escreve em
# gates, SL, TP, FVG↔CHoCH nem Telegram. Mesmo padrão já aprovado da
# simulação de cenários de gates (_passa_cenario_simulado/
# _simular_cenarios_gates) — recombina/filtra o que já é calculado,
# sem inventar nova detecção.
# ═══════════════════════════════════════════════════════════════════════

JANELAS_RECENCIA_HIPOTETICAS = [
    ('CURRENT', None), ('RECENCY_50', 50), ('RECENCY_100', 100),
    ('RECENCY_150', 150), ('RECENCY_200', 200),
]


def _avaliar_setup_recencia_hipotetica(candles_swing, candles_internal, idx_atual, max_idade_candles=None):
    """
    SIMULAÇÃO PURA — reproduz a MESMA cadeia de decisão já usada em
    avaliar_vortex_decision_layer() (bias -> zona -> CHoCH interno ->
    FVG), mas testando hipoteticamente um LIMITE DE IDADE do CHoCH
    (max_idade_candles). max_idade_candles=None reproduz o
    comportamento CURRENT (sem limite, idêntico ao já medido em
    medicao_idade_choch). NÃO altera avaliar_vortex_decision_layer()
    nem nenhuma função de decisão real — é uma função nova, isolada,
    só chamada por esta simulação comparativa.
    """
    resultado = {
        'bias': None, 'zona': None, 'zone_ok': False,
        'choch_existe_sem_filtro': False, 'choch_idade': None,
        'choch_valido_na_janela': False, 'choch_rejeitado_por_idade': False,
        'fvg_valido': False, 'motivo_rejeicao': None,
    }

    bias = compute_lux_structure_bias(candles_swing, swing_size=50)
    resultado['bias'] = bias
    if bias not in ('alta', 'baixa'):
        resultado['motivo_rejeicao'] = 'BIAS_FAIL'
        return resultado

    zona_calc = compute_lux_premium_discount(candles_swing, swing_size=50)
    preco_atual = candles_swing[-1]['c'] if candles_swing else None
    zona = classificar_zona_lux(preco_atual, zona_calc) if zona_calc and preco_atual is not None else None
    resultado['zona'] = zona
    zone_ok = (zona == 'discount') if bias == 'alta' else (zona == 'premium')
    resultado['zone_ok'] = zone_ok
    if not zone_ok:
        resultado['motivo_rejeicao'] = 'ZONE_FAIL'
        return resultado

    eventos_internos = compute_lux_internal_structure(candles_internal, swing_size=5)
    choch_relevante = None
    for ev in reversed(eventos_internos):
        if ev['tipo'] == 'CHoCH' and ev['direcao'] == bias:
            choch_relevante = ev
            break

    if not choch_relevante:
        resultado['motivo_rejeicao'] = 'NO_CHOCH'
        return resultado

    resultado['choch_existe_sem_filtro'] = True
    idade = idx_atual - choch_relevante['index']
    resultado['choch_idade'] = idade

    if max_idade_candles is not None and idade > max_idade_candles:
        resultado['choch_rejeitado_por_idade'] = True
        resultado['motivo_rejeicao'] = 'NO_CHOCH'  # tratado como ausência dentro da janela hipotética
        return resultado

    resultado['choch_valido_na_janela'] = True

    fvgs = find_open_fvgs_adaptive(candles_internal)
    tipo_fvg_desejado = 'FVG_bullish' if bias == 'alta' else 'FVG_bearish'
    candidatos = [f for f in fvgs if f['tipo'] == tipo_fvg_desejado]
    resultado['fvg_valido'] = len(candidatos) > 0
    if not candidatos:
        resultado['motivo_rejeicao'] = 'NO_VALID_FVG'
        return resultado

    resultado['motivo_rejeicao'] = 'FVG_CHOCH_RELATION_UNKNOWN'
    return resultado


def simular_recencia_choch_comparativo(pair, dias_historico=7):
    """
    Replay SOMENTE LEITURA, candle a candle, sem lookahead (mesmo
    mecanismo causal já validado). Roda as 5 janelas hipotéticas
    (CURRENT + RECENCY_50/100/150/200) sobre EXATAMENTE os mesmos
    ciclos, e compara. Não escreve em produção, não altera nenhuma
    regra existente.
    """
    symbol_map = {
        'BTCUSD': 'BTCUSDT', 'ETHUSD': 'ETHUSDT', 'SOLUSD': 'SOLUSDT', 'XRPUSD': 'XRPUSDT',
        'LINKUSD': 'LINKUSDT', 'ADAUSD': 'ADAUSDT', 'AVAXUSD': 'AVAXUSDT', 'BNBUSD': 'BNBUSDT',
        'AAVEUSD': 'AAVEUSDT', 'NEARUSD': 'NEARUSDT', 'PENDLEUSD': 'PENDLEUSDT', 'INJUSD': 'INJUSDT',
        'ONDOUSD': 'ONDOUSDT',
    }
    symbol = symbol_map.get(pair.upper(), pair.upper().replace('USD', 'USDT'))

    m5_bruto = _fetch_bybit_klines_historico(symbol, '5', dias_historico + 2)
    m5, validacao_m5 = _validar_e_limpar_candles(m5_bruto, '5')

    MIN_CANDLES_SWING_LOCAL = 55
    if len(m5) < MIN_CANDLES_SWING_LOCAL + 10:
        return {
            'erro': f'dados históricos insuficientes pra {pair} (M5={len(m5)})',
            'validacao_m5': validacao_m5,
        }

    agregados = {
        label: {
            'total_ciclos': 0, 'choch_validos': 0, 'choch_rejeitados_por_idade': 0,
            'zone_ok': 0, 'zone_fail': 0, 'fvg_valido': 0, 'fvg_ausente': 0,
            'setups_fvg_choch_relation_unknown': 0,
        }
        for label, _ in JANELAS_RECENCIA_HIPOTETICAS
    }

    current_setup_idades = []  # idade do CHoCH nos ciclos que hoje chegam a FVG_CHOCH_RELATION_UNKNOWN

    for i in range(MIN_CANDLES_SWING_LOCAL, len(m5)):
        candles_ate_agora = m5[:i + 1]

        for label, max_idade in JANELAS_RECENCIA_HIPOTETICAS:
            try:
                r = _avaliar_setup_recencia_hipotetica(candles_ate_agora, candles_ate_agora, i, max_idade_candles=max_idade)
            except Exception:
                continue

            ag = agregados[label]
            ag['total_ciclos'] += 1
            if r['zone_ok']:
                ag['zone_ok'] += 1
            elif r['bias'] in ('alta', 'baixa'):
                ag['zone_fail'] += 1

            if r['choch_valido_na_janela']:
                ag['choch_validos'] += 1
            elif r['choch_rejeitado_por_idade']:
                ag['choch_rejeitados_por_idade'] += 1

            if r['choch_valido_na_janela']:
                if r['fvg_valido']:
                    ag['fvg_valido'] += 1
                else:
                    ag['fvg_ausente'] += 1

            if r['motivo_rejeicao'] == 'FVG_CHOCH_RELATION_UNKNOWN':
                ag['setups_fvg_choch_relation_unknown'] += 1
                if label == 'CURRENT':
                    current_setup_idades.append(r['choch_idade'])

    # ── Tabela pedida: dos setups CURRENT, quantos sobrevivem em cada janela ──
    tabela_setups_preservados = {}
    for label, max_idade in JANELAS_RECENCIA_HIPOTETICAS:
        if max_idade is None:
            sobreviventes = len(current_setup_idades)
        else:
            sobreviventes = sum(1 for idade in current_setup_idades if idade <= max_idade)
        tabela_setups_preservados[label] = {
            'setups_finais': sobreviventes,
            'perdidos_vs_current': len(current_setup_idades) - sobreviventes,
        }

    return {
        'pair': pair, 'dias_historico': dias_historico,
        'nota_metodologica': (
            'SIMULAÇÃO/REPLAY PURO — nenhuma regra de produção foi tocada. Reutiliza as mesmas '
            'funções já existentes (compute_lux_structure_bias, compute_lux_premium_discount, '
            'compute_lux_internal_structure, find_open_fvgs_adaptive), sem alterar nenhuma. '
            'CURRENT = comportamento sem limite de idade (idêntico ao já medido). RECENCY_N = '
            'mesmo cenário, só rejeitando o CHoCH quando idade > N candles. "setups_finais" na '
            'tabela é calculado sobre o MESMO conjunto de ciclos que hoje chegam a '
            'FVG_CHOCH_RELATION_UNKNOWN no cenário CURRENT — filtrando apenas pela idade do '
            'CHoCH de cada um (bias/zona/FVG não dependem da recência, então não mudam entre '
            'cenários para o mesmo ciclo).'
        ),
        'agregados_por_janela': agregados,
        'tabela_setups_atuais_preservados': tabela_setups_preservados,
        'total_setups_current': len(current_setup_idades),
    }


def _executar_simulacao_recencia_job(db_file, job_id, pair, dias_historico):
    """Mesmo padrão de _executar_replay_comparativo_job — roda em
    background, persiste resultado/erro na tabela scalp_replay_jobs."""
    try:
        resultado = simular_recencia_choch_comparativo(pair, dias_historico=dias_historico)
        status_final = 'erro' if (isinstance(resultado, dict) and 'erro' in resultado) else 'concluido'
        with sqlite3.connect(db_file) as conn:
            conn.execute('''
                UPDATE scalp_replay_jobs
                SET status=?, resultado_json=?, finished_at=?
                WHERE job_id=?
            ''', (status_final, json.dumps(resultado, ensure_ascii=False), int(time.time()), job_id))
            conn.commit()
    except Exception as e:
        try:
            with sqlite3.connect(db_file) as conn:
                conn.execute('''
                    UPDATE scalp_replay_jobs
                    SET status='erro', erro=?, finished_at=?
                    WHERE job_id=?
                ''', (str(e), int(time.time()), job_id))
                conn.commit()
        except Exception as e2:
            print(f"[scalp_engine replay_jobs] erro ao registrar falha do job {job_id}: {e2}")


@explicacao_bp.route("/scalp_gates_vortex/simular_recencia_choch_iniciar", methods=["GET"])
def simular_recencia_choch_iniciar_endpoint():
    """
    Inicia simular_recencia_choch_comparativo() em BACKGROUND — mesmo
    padrão do replay comparativo. Devolve job_id na hora. Consultar
    resultado no MESMO endpoint de status já existente:
    /scalp_gates_vortex/replay_comparativo_luxalgo_status/<job_id>
    (o endpoint de status é genérico, lê qualquer job_id da tabela
    scalp_replay_jobs, não importa o tipo).
    Uso: ?pair=ADAUSD&dias=7&confirm=RODAR_SIMULACAO_RECENCIA
    """
    if request.args.get('confirm') != 'RODAR_SIMULACAO_RECENCIA':
        return jsonify({
            "erro": "endpoint pesado, protegido contra chamada acidental",
            "como_usar": "adiciona &confirm=RODAR_SIMULACAO_RECENCIA na URL, ex: "
                          "/scalp_gates_vortex/simular_recencia_choch_iniciar?pair=ADAUSD&dias=7&confirm=RODAR_SIMULACAO_RECENCIA",
        }), 400

    pair = request.args.get('pair', 'ADAUSD')
    dias = int(request.args.get('dias', 7))
    db_file = _db_file_explicacao()
    init_replay_jobs_db(db_file)

    job_id = f"simrecencia_{pair}_{int(time.time()*1000)}"

    try:
        with sqlite3.connect(db_file) as conn:
            conn.execute('''
                INSERT INTO scalp_replay_jobs (job_id, tipo, pair, dias_historico, status, created_at)
                VALUES (?, ?, ?, ?, 'rodando', ?)
            ''', (job_id, 'simular_recencia_choch_comparativo', pair, dias, int(time.time())))
            conn.commit()
    except Exception as e:
        return jsonify({"erro": f"não foi possível criar o job: {e}"}), 500

    thread = threading.Thread(
        target=_executar_simulacao_recencia_job,
        args=(db_file, job_id, pair, dias),
        daemon=True,
    )
    thread.start()

    return jsonify({
        "job_id": job_id,
        "status": "iniciado",
        "pair": pair,
        "dias_historico": dias,
        "como_consultar": f"/scalp_gates_vortex/replay_comparativo_luxalgo_status/{job_id}",
        "aviso": "roda em background — pode levar alguns minutos, consulte o job_id acima quando quiser",
    })


# ═══════════════════════════════════════════════════════════════════════
# VALIDAÇÃO MULTI-PAR DA RECÊNCIA DO CHoCH — item aprovado do ticket.
# Orquestra simular_recencia_choch_comparativo() (SEM ALTERAÇÃO
# NENHUMA) em sequência pra cada par monitorado, e monta a tabela
# consolidada + destaque de divergência vs BTCUSD (benchmark
# principal). Não recalcula nenhuma lógica de decisão — só soma/agrega
# o que cada chamada por par já devolve.
# ═══════════════════════════════════════════════════════════════════════

def simular_recencia_choch_todos_pares(dias_historico=7, pares=None):
    """
    Roda simular_recencia_choch_comparativo() (sem alteração) em
    sequência pra cada par da lista, e agrega os resultados numa
    tabela consolidada. Cada par é 100% independente — erro num par
    não derruba os demais. Mesma metodologia, mesmas garantias de
    segurança (replay puro, sem lookahead, sem tocar produção).
    """
    pares = pares or PARES_MONITORADOS_REPLAY
    resultados_por_pair = {}

    for p in pares:
        try:
            r = simular_recencia_choch_comparativo(p, dias_historico=dias_historico)
        except Exception as e:
            resultados_por_pair[p] = {'erro': str(e)}
            continue
        resultados_por_pair[p] = r

    tabela_consolidada = {}
    totais_setups_por_janela = {label: 0 for label, _ in JANELAS_RECENCIA_HIPOTETICAS}
    choch_validos_totais = {label: 0 for label, _ in JANELAS_RECENCIA_HIPOTETICAS}
    choch_rejeitados_totais = {label: 0 for label, _ in JANELAS_RECENCIA_HIPOTETICAS}
    zone_ok_total = 0
    zone_fail_total = 0
    total_ciclos_total = 0
    pares_com_erro = []

    for p, r in resultados_por_pair.items():
        if 'erro' in r:
            pares_com_erro.append(p)
            continue

        tabela_par = r.get('tabela_setups_atuais_preservados', {})
        tabela_consolidada[p] = {
            label: (tabela_par.get(label) or {}).get('setups_finais')
            for label, _ in JANELAS_RECENCIA_HIPOTETICAS
        }

        ag = r.get('agregados_por_janela', {})
        for label, _ in JANELAS_RECENCIA_HIPOTETICAS:
            bloco = ag.get(label, {})
            totais_setups_por_janela[label] += bloco.get('setups_fvg_choch_relation_unknown', 0) or 0
            choch_validos_totais[label] += bloco.get('choch_validos', 0) or 0
            choch_rejeitados_totais[label] += bloco.get('choch_rejeitados_por_idade', 0) or 0

        current_bloco = ag.get('CURRENT', {})
        zone_ok_total += current_bloco.get('zone_ok', 0) or 0
        zone_fail_total += current_bloco.get('zone_fail', 0) or 0
        total_ciclos_total += current_bloco.get('total_ciclos', 0) or 0

    total_current_global = totais_setups_por_janela.get('CURRENT', 0)
    percentual_preservado_global = {}
    setups_perdidos_global = {}
    for label, _ in JANELAS_RECENCIA_HIPOTETICAS:
        n = totais_setups_por_janela[label]
        percentual_preservado_global[label] = round(100 * n / total_current_global, 1) if total_current_global else None
        setups_perdidos_global[label] = total_current_global - n

    # ── Destaque de divergência vs BTCUSD (benchmark principal),
    # pedido explícito do ticket — puramente comparativo, não decide
    # qual janela usar. ──
    divergencia_vs_btc = {}
    btc_tabela = tabela_consolidada.get('BTCUSD', {})
    if btc_tabela:
        for p, tabela_p in tabela_consolidada.items():
            if p == 'BTCUSD':
                continue
            diffs = {}
            for label, _ in JANELAS_RECENCIA_HIPOTETICAS:
                btc_v = btc_tabela.get(label)
                p_v = tabela_p.get(label)
                if btc_v is not None and p_v is not None:
                    diffs[label] = p_v - btc_v
            divergencia_vs_btc[p] = diffs

    return {
        'dias_historico': dias_historico,
        'pares_testados': pares,
        'pares_com_erro': pares_com_erro,
        'benchmark_principal': 'BTCUSD',
        'tabela_consolidada_setups_finais_por_par': tabela_consolidada,
        'consolidado_global': {
            'total_ciclos_somados_todos_pares': total_ciclos_total,
            'zone_ok_somado': zone_ok_total,
            'zone_fail_somado': zone_fail_total,
            'setups_totais_por_janela': totais_setups_por_janela,
            'choch_validos_totais_por_janela': choch_validos_totais,
            'choch_rejeitados_totais_por_janela': choch_rejeitados_totais,
            'setups_perdidos_vs_current_por_janela': setups_perdidos_global,
            'percentual_preservado_por_janela': percentual_preservado_global,
            'nota_distribuicao_idade': (
                'choch_validos_totais_por_janela e choch_rejeitados_totais_por_janela, somados '
                'de todos os pares, funcionam como um proxy agregado da distribuição de idade '
                '(quantos CHoCH sobrevivem em cada corte). Para percentis/estatística de idade '
                'RAW cruzando todos os pares (como já feito individualmente pra ADAUSD via '
                'medicao_idade_choch), seria necessário rodar replay_comparativo_luxalgo() '
                'também por par — não incluído aqui pra não dobrar o tempo de execução; '
                'disponível como próximo passo se for necessário.'
            ),
        },
        'divergencia_vs_btc_por_par': divergencia_vs_btc,
        'nota_metodologica': (
            'Cada par foi processado de forma totalmente independente, chamando '
            'simular_recencia_choch_comparativo() sem nenhuma alteração — mesma metodologia, '
            'mesmas garantias (replay puro, sem lookahead, sem tocar produção). Erro num par '
            'não derruba os demais. Nenhuma janela foi escolhida ou aplicada — só '
            'orquestração/agregação do que cada chamada por par já calcula.'
        ),
        'resultados_detalhados_por_pair': resultados_por_pair,
    }


def _executar_simulacao_recencia_todos_pares_job(db_file, job_id, dias_historico, pares):
    """Mesmo padrão de _executar_simulacao_recencia_job, só que pra
    todos os pares — roda em background, persiste resultado/erro."""
    try:
        resultado = simular_recencia_choch_todos_pares(dias_historico=dias_historico, pares=pares)
        with sqlite3.connect(db_file) as conn:
            conn.execute('''
                UPDATE scalp_replay_jobs
                SET status='concluido', resultado_json=?, finished_at=?
                WHERE job_id=?
            ''', (json.dumps(resultado, ensure_ascii=False), int(time.time()), job_id))
            conn.commit()
    except Exception as e:
        try:
            with sqlite3.connect(db_file) as conn:
                conn.execute('''
                    UPDATE scalp_replay_jobs
                    SET status='erro', erro=?, finished_at=?
                    WHERE job_id=?
                ''', (str(e), int(time.time()), job_id))
                conn.commit()
        except Exception as e2:
            print(f"[scalp_engine replay_jobs] erro ao registrar falha do job {job_id}: {e2}")


@explicacao_bp.route("/scalp_gates_vortex/simular_recencia_choch_todos_pares_iniciar", methods=["GET"])
def simular_recencia_choch_todos_pares_iniciar_endpoint():
    """
    Fase 2 do ticket — inicia simular_recencia_choch_todos_pares() em
    BACKGROUND (MUITO pesado, roda os 13 pares em sequência). Devolve
    job_id na hora. Consultar no MESMO endpoint de status genérico já
    existente: /scalp_gates_vortex/replay_comparativo_luxalgo_status/<job_id>
    Uso: ?dias=7&confirm=RODAR_SIMULACAO_RECENCIA_TODOS_PARES
    Opcional &pares=BTCUSD,ETHUSD,... pra rodar um subconjunto.
    """
    if request.args.get('confirm') != 'RODAR_SIMULACAO_RECENCIA_TODOS_PARES':
        return jsonify({
            "erro": "endpoint MUITO pesado (roda todos os pares em sequência), protegido contra chamada acidental",
            "como_usar": "adiciona &confirm=RODAR_SIMULACAO_RECENCIA_TODOS_PARES na URL, ex: "
                          "/scalp_gates_vortex/simular_recencia_choch_todos_pares_iniciar?dias=7&confirm=RODAR_SIMULACAO_RECENCIA_TODOS_PARES",
        }), 400

    dias = int(request.args.get('dias', 7))
    pares_param = request.args.get('pares')
    pares_lista = None
    if pares_param:
        pares_lista = [p.strip().upper() for p in pares_param.split(',') if p.strip()]

    db_file = _db_file_explicacao()
    init_replay_jobs_db(db_file)

    job_id = f"simrecenciatodos_{int(time.time()*1000)}"

    try:
        with sqlite3.connect(db_file) as conn:
            conn.execute('''
                INSERT INTO scalp_replay_jobs (job_id, tipo, pair, dias_historico, status, created_at)
                VALUES (?, ?, ?, ?, 'rodando', ?)
            ''', (job_id, 'simular_recencia_choch_todos_pares', 'TODOS', dias, int(time.time())))
            conn.commit()
    except Exception as e:
        return jsonify({"erro": f"não foi possível criar o job: {e}"}), 500

    thread = threading.Thread(
        target=_executar_simulacao_recencia_todos_pares_job,
        args=(db_file, job_id, dias, pares_lista),
        daemon=True,
    )
    thread.start()

    return jsonify({
        "job_id": job_id,
        "status": "iniciado",
        "dias_historico": dias,
        "pares": pares_lista or PARES_MONITORADOS_REPLAY,
        "como_consultar": f"/scalp_gates_vortex/replay_comparativo_luxalgo_status/{job_id}",
        "aviso": "MUITO pesado (13 pares em sequência) — pode levar bastante tempo, roda em background sem prazo de conexão",
    })


# ═══════════════════════════════════════════════════════════════════════
# ESTATÍSTICAS ENTRE PARES + BENCHMARK BTC — item aprovado do ticket.
# Wrapper PURAMENTE ADITIVO sobre simular_recencia_choch_todos_pares()
# (não alterada) — calcula percentual preservado por par/janela e as
# estatísticas entre pares (quantos preservam 100%, quantos perdem,
# pior/mediana/média de preservação), sem recalcular nenhuma
# simulação, só agregando aritmeticamente o que a função de base já
# produziu. Destaca BTCUSD como benchmark principal, conforme diretriz.
# ═══════════════════════════════════════════════════════════════════════

def _percentual_preservado_por_par(resultado_todos_pares):
    """
    Puramente aritmético — não chama nenhuma simulação nova. Lê
    resultados_detalhados_por_pair (já produzido por
    simular_recencia_choch_todos_pares) e calcula o percentual
    preservado por par/janela + estatísticas entre pares.
    """
    detalhes = resultado_todos_pares.get('resultados_detalhados_por_pair', {})
    tabela_pct = {}
    for p, r in detalhes.items():
        if 'erro' in r:
            continue
        tabela_setup = r.get('tabela_setups_atuais_preservados', {})
        total_current = r.get('total_setups_current', 0) or 0
        pct_por_janela = {}
        for label, _ in JANELAS_RECENCIA_HIPOTETICAS:
            setups_finais = (tabela_setup.get(label) or {}).get('setups_finais')
            if total_current > 0 and setups_finais is not None:
                pct_por_janela[label] = round(100 * setups_finais / total_current, 1)
            else:
                pct_por_janela[label] = None
        tabela_pct[p] = pct_por_janela

    estatisticas_entre_pares = {}
    for label, _ in JANELAS_RECENCIA_HIPOTETICAS:
        valores = [tabela_pct[p][label] for p in tabela_pct if tabela_pct[p].get(label) is not None]
        pares_100 = sum(1 for v in valores if v == 100.0)
        pares_com_perda = sum(1 for v in valores if v is not None and v < 100.0)
        pior = min(valores) if valores else None
        pior_par = None
        if pior is not None:
            for p in tabela_pct:
                if tabela_pct[p].get(label) == pior:
                    pior_par = p
                    break
        valores_ordenados = sorted(valores)
        n = len(valores_ordenados)
        mediana = None
        if n:
            meio = n // 2
            mediana = valores_ordenados[meio] if n % 2 == 1 else round((valores_ordenados[meio - 1] + valores_ordenados[meio]) / 2, 1)
        media = round(sum(valores) / n, 1) if n else None
        estatisticas_entre_pares[label] = {
            'pares_com_100_por_cento': pares_100,
            'pares_com_perda': pares_com_perda,
            'pior_preservacao_pct': pior,
            'pior_par': pior_par,
            'mediana_preservacao_pct': mediana,
            'media_preservacao_pct': media,
            'total_pares_validos': n,
        }

    return {
        'tabela_percentual_preservado_por_par': tabela_pct,
        'estatisticas_entre_pares_por_janela': estatisticas_entre_pares,
    }


def simular_recencia_choch_todos_pares_com_stats(dias_historico=7, pares=None):
    """
    Wrapper aditivo sobre simular_recencia_choch_todos_pares() (NÃO
    alterada) — chama a função de base uma única vez e agrega, por
    cima do resultado já calculado, o percentual preservado por
    par/janela + estatísticas entre pares + tabela BTC destacada como
    benchmark principal. Não recalcula nenhuma simulação nem toca em
    nenhuma função de decisão real.
    """
    resultado_base = simular_recencia_choch_todos_pares(dias_historico=dias_historico, pares=pares)
    if 'resultados_detalhados_por_pair' not in resultado_base:
        return resultado_base

    stats_extra = _percentual_preservado_por_par(resultado_base)

    btc_detalhe = (resultado_base.get('resultados_detalhados_por_pair') or {}).get('BTCUSD', {})
    btc_tabela_setup = btc_detalhe.get('tabela_setups_atuais_preservados', {})
    tabela_btc_benchmark = {
        label: {
            'setups_finais': (btc_tabela_setup.get(label) or {}).get('setups_finais'),
            'perdidos_vs_current': (btc_tabela_setup.get(label) or {}).get('perdidos_vs_current'),
            'percentual_preservado': stats_extra['tabela_percentual_preservado_por_par'].get('BTCUSD', {}).get(label),
        }
        for label, _ in JANELAS_RECENCIA_HIPOTETICAS
    }

    resultado_final = dict(resultado_base)
    resultado_final['tabela_percentual_preservado_por_par'] = stats_extra['tabela_percentual_preservado_por_par']
    resultado_final['estatisticas_entre_pares_por_janela'] = stats_extra['estatisticas_entre_pares_por_janela']
    resultado_final['tabela_btc_benchmark_destacada'] = tabela_btc_benchmark
    resultado_final['nota_diretriz'] = (
        'BTC é o benchmark principal (usar tabela_btc_benchmark_destacada para decisão '
        'primária). Os demais pares servem pra validar generalização — ver '
        'estatisticas_entre_pares_por_janela (pares_com_100_por_cento, pares_com_perda, '
        'pior_preservacao_pct, mediana, média). Nenhuma janela foi escolhida automaticamente '
        'aqui — só agregação estatística pra apoiar a decisão manual.'
    )
    return resultado_final


def _executar_simulacao_recencia_todos_pares_stats_job(db_file, job_id, dias_historico, pares):
    """Mesmo padrão dos outros jobs — roda em background, persiste
    resultado/erro na tabela scalp_replay_jobs."""
    try:
        resultado = simular_recencia_choch_todos_pares_com_stats(dias_historico=dias_historico, pares=pares)
        with sqlite3.connect(db_file) as conn:
            conn.execute('''
                UPDATE scalp_replay_jobs
                SET status='concluido', resultado_json=?, finished_at=?
                WHERE job_id=?
            ''', (json.dumps(resultado, ensure_ascii=False), int(time.time()), job_id))
            conn.commit()
    except Exception as e:
        try:
            with sqlite3.connect(db_file) as conn:
                conn.execute('''
                    UPDATE scalp_replay_jobs
                    SET status='erro', erro=?, finished_at=?
                    WHERE job_id=?
                ''', (str(e), int(time.time()), job_id))
                conn.commit()
        except Exception as e2:
            print(f"[scalp_engine replay_jobs] erro ao registrar falha do job {job_id}: {e2}")


@explicacao_bp.route("/scalp_gates_vortex/simular_recencia_choch_benchmark_btc_iniciar", methods=["GET"])
def simular_recencia_choch_benchmark_btc_iniciar_endpoint():
    """
    Roda simular_recencia_choch_todos_pares_com_stats() em BACKGROUND
    — versão com estatísticas entre pares e BTC destacado como
    benchmark, conforme diretriz aprovada. MUITO pesado (13 pares em
    sequência, cada um pode levar dezenas de minutos em dias=30).
    Consultar no MESMO endpoint de status genérico já existente.
    Uso: ?dias=7&confirm=RODAR_BENCHMARK_BTC (recomendado começar com
    dias=7 antes de tentar dias=30, dado o custo já medido).
    Opcional &pares=BTCUSD,ETHUSD,... pra subconjunto.
    """
    if request.args.get('confirm') != 'RODAR_BENCHMARK_BTC':
        return jsonify({
            "erro": "endpoint MUITO pesado, protegido contra chamada acidental",
            "como_usar": "adiciona &confirm=RODAR_BENCHMARK_BTC na URL, ex: "
                          "/scalp_gates_vortex/simular_recencia_choch_benchmark_btc_iniciar?dias=7&confirm=RODAR_BENCHMARK_BTC",
            "aviso": "recomendado começar com dias=7 (13 pares) antes de tentar dias=30 — "
                     "o job de BTC sozinho com 30 dias já levou dezenas de minutos",
        }), 400

    dias = int(request.args.get('dias', 7))
    pares_param = request.args.get('pares')
    pares_lista = None
    if pares_param:
        pares_lista = [p.strip().upper() for p in pares_param.split(',') if p.strip()]

    db_file = _db_file_explicacao()
    init_replay_jobs_db(db_file)

    job_id = f"benchmarkbtc_{int(time.time()*1000)}"

    try:
        with sqlite3.connect(db_file) as conn:
            conn.execute('''
                INSERT INTO scalp_replay_jobs (job_id, tipo, pair, dias_historico, status, created_at)
                VALUES (?, ?, ?, ?, 'rodando', ?)
            ''', (job_id, 'simular_recencia_choch_todos_pares_com_stats', 'TODOS', dias, int(time.time())))
            conn.commit()
    except Exception as e:
        return jsonify({"erro": f"não foi possível criar o job: {e}"}), 500

    thread = threading.Thread(
        target=_executar_simulacao_recencia_todos_pares_stats_job,
        args=(db_file, job_id, dias, pares_lista),
        daemon=True,
    )
    thread.start()

    return jsonify({
        "job_id": job_id,
        "status": "iniciado",
        "dias_historico": dias,
        "pares": pares_lista or PARES_MONITORADOS_REPLAY,
        "como_consultar": f"/scalp_gates_vortex/replay_comparativo_luxalgo_status/{job_id}",
        "aviso": "MUITO pesado — roda em background sem prazo de conexão, mas pode levar bastante tempo",
    })


# ═══════════════════════════════════════════════════════════════════════
# AUDITORIA DE RELEVÂNCIA DO CHoCH (TOQUE/CONSUMO/INVALIDAÇÃO) — item
# aprovado do ticket. SOMENTE REPLAY/AUDITORIA, não altera nenhuma
# regra de produção. Busca no código existente ANTES de inventar:
# find_open_fvgs()/find_open_fvgs_adaptive() têm 'preenchida' (FVG
# mitigada) e find_order_blocks_com_mitigacao() tem 'mitigado' — ambos
# operam sobre ZONAS (top/bottom). O nível de um CHoCH (ev['nivel']) é
# um preço ÚNICO, não uma zona — nenhuma das duas se aplica diretamente.
# Por isso as funções abaixo são uma DEFINIÇÃO NOVA DE AUDITORIA,
# EXPLICITAMENTE EXPERIMENTAL, inspirada no mesmo princípio geométrico
# já usado (candle tocando um nível) — NÃO é lógica de produção, não é
# reaproveitamento de detector existente, e é marcada como tal em todo
# o relatório.
# ═══════════════════════════════════════════════════════════════════════

def _evento_choch_foi_invalidado(eventos_internos, choch_evento, idx_atual):
    """
    Busca, na MESMA lista de eventos já calculada por
    compute_lux_internal_structure() (não recalcula nada), se existe
    um evento estrutural (BOS ou CHoCH) de direção OPOSTA à do CHoCH
    avaliado, ocorrido DEPOIS dele (index > choch_evento['index']) e
    até idx_atual (nunca depois — sem lookahead). Se existir, o CHoCH
    original foi estruturalmente superado por um movimento contrário.
    """
    for ev in eventos_internos:
        if ev['index'] <= choch_evento['index']:
            continue
        if ev['index'] > idx_atual:
            continue
        if ev['tipo'] in ('BOS', 'CHoCH') and ev['direcao'] != choch_evento['direcao']:
            return True, ev
    return False, None


def _contar_toques_nivel(candles_internal, nivel, idx_inicio, idx_fim):
    """
    Conta quantos candles, entre idx_inicio e idx_fim (inclusive),
    tiveram seu range (low-high) cruzando o nível informado — mesmo
    princípio geométrico do 'preenchida'/'mitigado' já usados no
    engine pra zonas, aplicado aqui a um preço único. Sem lookahead:
    quem chama já garante idx_fim <= idx_atual do ciclo.
    """
    toques = 0
    n = len(candles_internal)
    for i in range(idx_inicio, idx_fim + 1):
        if i < 0 or i >= n:
            continue
        c = candles_internal[i]
        if c['l'] <= nivel <= c['h']:
            toques += 1
    return toques


def classificar_estado_choch_auditoria(candles_internal, eventos_internos, choch_evento, idx_atual):
    """
    CLASSIFICAÇÃO EXPERIMENTAL DE AUDITORIA (não é lógica de
    produção). Regras mutuamente exclusivas, em ordem de prioridade,
    usando só candles/eventos com index/timestamp <= idx_atual (sem
    lookahead):

    1. INVALIDADO — existe evento estrutural (BOS ou CHoCH) de direção
       OPOSTA na mesma estrutura interna, depois do CHoCH avaliado.
    2. NOVO — nenhum candle entre choch_index+1 e idx_atual tocou
       (high>=nivel>=low) o nível do CHoCH.
    3. RETESTADO — tocou o nível exatamente 1 vez, sem invalidação.
    4. CONSUMIDO — tocou o nível 2+ vezes, sem invalidação.

    'ainda_valido' é um booleano PARALELO (não uma 5ª categoria
    exclusiva) = not invalidado — ou seja, NOVO/RETESTADO/CONSUMIDO
    contam todos como "ainda válido"; só INVALIDADO não conta.
    """
    idx_choch = choch_evento['index']
    invalidado, evento_invalidador = _evento_choch_foi_invalidado(eventos_internos, choch_evento, idx_atual)
    toques = _contar_toques_nivel(candles_internal, choch_evento['nivel'], idx_choch + 1, idx_atual)

    if invalidado:
        estado = 'INVALIDADO'
    elif toques == 0:
        estado = 'NOVO'
    elif toques == 1:
        estado = 'RETESTADO'
    else:
        estado = 'CONSUMIDO'

    return {
        'estado': estado,
        'ainda_valido': not invalidado,
        'foi_tocado': toques > 0,
        'numero_de_toques': toques,
        'foi_invalidado': invalidado,
        'evento_invalidador': evento_invalidador,
    }


def auditar_relevancia_choch(pair, dias_historico=7):
    """
    Replay SOMENTE LEITURA, mesma metodologia causal já aprovada (sem
    lookahead, candles_ate_agora = m5[:i+1]). Pra cada ciclo com CHoCH
    interno presente (mesma seleção já usada em diagnostico_
    independente_luxalgo/avaliar_vortex_decision_layer), classifica o
    estado (NOVO/RETESTADO/CONSUMIDO/INVALIDADO) e relaciona com os
    setups CURRENT (mesmo critério de FVG_CHOCH_RELATION_UNKNOWN já
    usado). Não chama nenhuma função de decisão real, não altera nada.
    """
    symbol_map = {
        'BTCUSD': 'BTCUSDT', 'ETHUSD': 'ETHUSDT', 'SOLUSD': 'SOLUSDT', 'XRPUSD': 'XRPUSDT',
        'LINKUSD': 'LINKUSDT', 'ADAUSD': 'ADAUSDT', 'AVAXUSD': 'AVAXUSDT', 'BNBUSD': 'BNBUSDT',
        'AAVEUSD': 'AAVEUSDT', 'NEARUSD': 'NEARUSDT', 'PENDLEUSD': 'PENDLEUSDT', 'INJUSD': 'INJUSDT',
        'ONDOUSD': 'ONDOUSDT',
    }
    symbol = symbol_map.get(pair.upper(), pair.upper().replace('USD', 'USDT'))

    m5_bruto = _fetch_bybit_klines_historico(symbol, '5', dias_historico + 2)
    m5, validacao_m5 = _validar_e_limpar_candles(m5_bruto, '5')

    MIN_CANDLES_SWING_LOCAL = 55
    if len(m5) < MIN_CANDLES_SWING_LOCAL + 10:
        return {'erro': f'dados históricos insuficientes pra {pair} (M5={len(m5)})', 'validacao_m5': validacao_m5}

    todos_choch_observados = []  # 1 registro por (choch_index, direcao) único observado — não por ciclo
    choch_ja_registrado = set()
    setups_current = []
    setups_perdidos_por_janela = {'RECENCY_100': [], 'RECENCY_150': [], 'RECENCY_200': []}

    for i in range(MIN_CANDLES_SWING_LOCAL, len(m5)):
        candles_ate_agora = m5[:i + 1]

        bias = compute_lux_structure_bias(candles_ate_agora, swing_size=50)
        if bias not in ('alta', 'baixa'):
            continue

        eventos_internos = compute_lux_internal_structure(candles_ate_agora, swing_size=5)
        choch_relevante = None
        for ev in reversed(eventos_internos):
            if ev['tipo'] == 'CHoCH' and ev['direcao'] == bias:
                choch_relevante = ev
                break
        if not choch_relevante:
            continue

        idade = i - choch_relevante['index']
        chave_choch = (choch_relevante['index'], choch_relevante['direcao'])

        estado_info = classificar_estado_choch_auditoria(candles_ate_agora, eventos_internos, choch_relevante, i)

        if chave_choch not in choch_ja_registrado:
            choch_ja_registrado.add(chave_choch)
            todos_choch_observados.append({
                'pair': pair, 'choch_index': choch_relevante['index'], 'choch_ts': choch_relevante['t'],
                'choch_nivel': choch_relevante['nivel'], 'direcao': choch_relevante['direcao'],
                'primeira_idade_observada': idade, 'primeiro_idx_ciclo_observado': i,
                'estado_na_primeira_observacao': estado_info['estado'],
            })

        # ── Zona + FVG, mesmo critério já usado em avaliar_vortex_decision_layer ──
        zona_calc = compute_lux_premium_discount(candles_ate_agora, swing_size=50)
        preco_atual = candles_ate_agora[-1]['c']
        zona = classificar_zona_lux(preco_atual, zona_calc) if zona_calc else None
        zone_ok = (zona == 'discount') if bias == 'alta' else (zona == 'premium')
        if not zone_ok:
            continue

        fvgs = find_open_fvgs_adaptive(candles_ate_agora)
        tipo_fvg_desejado = 'FVG_bullish' if bias == 'alta' else 'FVG_bearish'
        candidatos_fvg = [f for f in fvgs if f['tipo'] == tipo_fvg_desejado]
        if not candidatos_fvg:
            continue

        # ── Chegou até FVG_CHOCH_RELATION_UNKNOWN — é um setup CURRENT ──
        registro_setup = {
            'pair': pair, 'candle_event_ts': candles_ate_agora[-1]['t'], 'idx_ciclo': i,
            'direcao': bias, 'idade_choch': idade,
            'choch_index': choch_relevante['index'], 'choch_ts': choch_relevante['t'],
            'choch_nivel': choch_relevante['nivel'],
            'estado_choch': estado_info['estado'], 'ainda_valido': estado_info['ainda_valido'],
            'foi_tocado': estado_info['foi_tocado'], 'numero_de_toques': estado_info['numero_de_toques'],
            'foi_invalidado': estado_info['foi_invalidado'],
            'evento_invalidador': estado_info['evento_invalidador'],
            'zona': zona, 'fvg_candidatos_n': len(candidatos_fvg),
            'fvg_mais_proximo': min(candidatos_fvg, key=lambda f: abs((f['top'] + f['bottom']) / 2 - preco_atual)),
            'eliminado_por_RECENCY_100': idade > 100,
            'eliminado_por_RECENCY_150': idade > 150,
            'eliminado_por_RECENCY_200': idade > 200,
        }
        setups_current.append(registro_setup)
        for label, limite in (('RECENCY_100', 100), ('RECENCY_150', 150), ('RECENCY_200', 200)):
            if idade > limite:
                setups_perdidos_por_janela[label].append(registro_setup)

    return {
        'pair': pair, 'dias_historico': dias_historico,
        'nota_metodologica': (
            'SOMENTE REPLAY/AUDITORIA — nenhuma regra de produção foi tocada, nenhum gate, '
            'CHoCH, FVG, Premium/Discount, SL, TP, R:R ou lógica de sinal foi alterada. Mesma '
            'metodologia causal já aprovada (sem lookahead — candles_ate_agora = m5[:i+1] em '
            'cada ciclo, classificar_estado_choch_auditoria() só usa candles/eventos com index '
            '<= idx_atual do ciclo). A classificação NOVO/RETESTADO/CONSUMIDO/INVALIDADO é uma '
            'DEFINIÇÃO EXPERIMENTAL DE AUDITORIA — não existe no código de produção um conceito '
            'equivalente aplicado a um NÍVEL de CHoCH (só existe para ZONAS: FVG "preenchida" e '
            'Order Block "mitigado"). Não é lógica de produção, não decide nada, não é usada por '
            'nenhuma função de decisão real.'
        ),
        'todos_choch_observados': todos_choch_observados,
        'setups_current': setups_current,
        'setups_perdidos_por_janela': setups_perdidos_por_janela,
        'total_choch_unicos_observados': len(todos_choch_observados),
        'total_setups_current': len(setups_current),
        'm5_completo': m5,
    }


def _executar_auditoria_relevancia_choch_job(db_file, job_id, pair, dias_historico):
    """Mesmo padrão dos outros jobs — roda em background, persiste
    resultado/erro na tabela scalp_replay_jobs."""
    try:
        resultado = auditar_relevancia_choch(pair, dias_historico=dias_historico)
        status_final = 'erro' if (isinstance(resultado, dict) and 'erro' in resultado) else 'concluido'
        with sqlite3.connect(db_file) as conn:
            conn.execute('''
                UPDATE scalp_replay_jobs
                SET status=?, resultado_json=?, finished_at=?
                WHERE job_id=?
            ''', (status_final, json.dumps(resultado, ensure_ascii=False), int(time.time()), job_id))
            conn.commit()
    except Exception as e:
        try:
            with sqlite3.connect(db_file) as conn:
                conn.execute('''
                    UPDATE scalp_replay_jobs
                    SET status='erro', erro=?, finished_at=?
                    WHERE job_id=?
                ''', (str(e), int(time.time()), job_id))
                conn.commit()
        except Exception as e2:
            print(f"[scalp_engine replay_jobs] erro ao registrar falha do job {job_id}: {e2}")


def _executar_auditoria_relevancia_choch_todos_pares_job(db_file, job_id, dias_historico, pares):
    """Roda auditar_relevancia_choch() (sem alteração) em sequência
    pra cada par, agregando os resultados num único dict. Mesmo padrão
    de isolamento de erro dos outros jobs multi-par."""
    pares = pares or PARES_MONITORADOS_REPLAY
    try:
        resultados_por_pair = {}
        for p in pares:
            try:
                resultados_por_pair[p] = auditar_relevancia_choch(p, dias_historico=dias_historico)
            except Exception as e:
                resultados_por_pair[p] = {'erro': str(e)}

        resultado = {
            'dias_historico': dias_historico, 'pares_testados': pares,
            'benchmark_principal': 'BTCUSD',
            'resultados_por_pair': resultados_por_pair,
        }
        with sqlite3.connect(db_file) as conn:
            conn.execute('''
                UPDATE scalp_replay_jobs
                SET status='concluido', resultado_json=?, finished_at=?
                WHERE job_id=?
            ''', (json.dumps(resultado, ensure_ascii=False), int(time.time()), job_id))
            conn.commit()
    except Exception as e:
        try:
            with sqlite3.connect(db_file) as conn:
                conn.execute('''
                    UPDATE scalp_replay_jobs
                    SET status='erro', erro=?, finished_at=?
                    WHERE job_id=?
                ''', (str(e), int(time.time()), job_id))
                conn.commit()
        except Exception as e2:
            print(f"[scalp_engine replay_jobs] erro ao registrar falha do job {job_id}: {e2}")


@explicacao_bp.route("/scalp_gates_vortex/auditar_relevancia_choch_iniciar", methods=["GET"])
def auditar_relevancia_choch_iniciar_endpoint():
    """
    Roda auditar_relevancia_choch() em BACKGROUND, 1 par. Consultar no
    MESMO endpoint de status genérico já existente.
    Uso: ?pair=BTCUSD&dias=7&confirm=RODAR_AUDITORIA_CHOCH
    """
    if request.args.get('confirm') != 'RODAR_AUDITORIA_CHOCH':
        return jsonify({
            "erro": "endpoint pesado, protegido contra chamada acidental",
            "como_usar": "adiciona &confirm=RODAR_AUDITORIA_CHOCH na URL, ex: "
                          "/scalp_gates_vortex/auditar_relevancia_choch_iniciar?pair=BTCUSD&dias=7&confirm=RODAR_AUDITORIA_CHOCH",
        }), 400

    pair = request.args.get('pair', 'BTCUSD')
    dias = int(request.args.get('dias', 7))
    db_file = _db_file_explicacao()
    init_replay_jobs_db(db_file)
    job_id = f"auditchoch_{pair}_{int(time.time()*1000)}"

    try:
        with sqlite3.connect(db_file) as conn:
            conn.execute('''
                INSERT INTO scalp_replay_jobs (job_id, tipo, pair, dias_historico, status, created_at)
                VALUES (?, ?, ?, ?, 'rodando', ?)
            ''', (job_id, 'auditar_relevancia_choch', pair, dias, int(time.time())))
            conn.commit()
    except Exception as e:
        return jsonify({"erro": f"não foi possível criar o job: {e}"}), 500

    thread = threading.Thread(target=_executar_auditoria_relevancia_choch_job, args=(db_file, job_id, pair, dias), daemon=True)
    thread.start()

    return jsonify({
        "job_id": job_id, "status": "iniciado", "pair": pair, "dias_historico": dias,
        "como_consultar": f"/scalp_gates_vortex/replay_comparativo_luxalgo_status/{job_id}",
        "aviso": "roda em background — pode levar alguns minutos",
    })


@explicacao_bp.route("/scalp_gates_vortex/auditar_relevancia_choch_todos_pares_iniciar", methods=["GET"])
def auditar_relevancia_choch_todos_pares_iniciar_endpoint():
    """
    Roda auditar_relevancia_choch() em BACKGROUND pra todos os pares.
    Consultar no MESMO endpoint de status genérico já existente.
    Uso: ?dias=7&confirm=RODAR_AUDITORIA_CHOCH_TODOS_PARES
    Opcional &pares=BTCUSD,ETHUSD,...
    """
    if request.args.get('confirm') != 'RODAR_AUDITORIA_CHOCH_TODOS_PARES':
        return jsonify({
            "erro": "endpoint MUITO pesado, protegido contra chamada acidental",
            "como_usar": "adiciona &confirm=RODAR_AUDITORIA_CHOCH_TODOS_PARES na URL, ex: "
                          "/scalp_gates_vortex/auditar_relevancia_choch_todos_pares_iniciar?dias=7&confirm=RODAR_AUDITORIA_CHOCH_TODOS_PARES",
        }), 400

    dias = int(request.args.get('dias', 7))
    pares_param = request.args.get('pares')
    pares_lista = None
    if pares_param:
        pares_lista = [p.strip().upper() for p in pares_param.split(',') if p.strip()]

    db_file = _db_file_explicacao()
    init_replay_jobs_db(db_file)
    job_id = f"auditchochtodos_{int(time.time()*1000)}"

    try:
        with sqlite3.connect(db_file) as conn:
            conn.execute('''
                INSERT INTO scalp_replay_jobs (job_id, tipo, pair, dias_historico, status, created_at)
                VALUES (?, ?, ?, ?, 'rodando', ?)
            ''', (job_id, 'auditar_relevancia_choch_todos_pares', 'TODOS', dias, int(time.time())))
            conn.commit()
    except Exception as e:
        return jsonify({"erro": f"não foi possível criar o job: {e}"}), 500

    thread = threading.Thread(
        target=_executar_auditoria_relevancia_choch_todos_pares_job,
        args=(db_file, job_id, dias, pares_lista), daemon=True,
    )
    thread.start()

    return jsonify({
        "job_id": job_id, "status": "iniciado", "dias_historico": dias,
        "pares": pares_lista or PARES_MONITORADOS_REPLAY,
        "como_consultar": f"/scalp_gates_vortex/replay_comparativo_luxalgo_status/{job_id}",
        "aviso": "MUITO pesado — roda em background sem prazo de conexão, mas pode levar bastante tempo",
    })


# ═══════════════════════════════════════════════════════════════════════
# FILTRO EXPERIMENTAL CHoCH VIVO/MORTO — item aprovado do ticket.
# SOMENTE TESTE/REPLAY/PAPER, sem deploy, sem alterar produção. NÃO
# recalcula estrutura nenhuma — reaproveita exclusivamente o campo
# estado_choch já calculado por classificar_estado_choch_auditoria()
# dentro de auditar_relevancia_choch() (causal, sem lookahead, já
# testado e aprovado). Não substitui nenhum gate, não altera CHoCH,
# FVG, Premium/Discount, MSS/BOS, SL, TP, R:R ou FVG↔CHoCH. Não é
# chamado por nenhum caminho de produção.
# ═══════════════════════════════════════════════════════════════════════

def aplicar_filtro_choch_vivo(setup):
    """
    Classifica um setup JÁ PRODUZIDO por auditar_relevancia_choch()
    (reaproveita setup['estado_choch'], não recalcula nada) em:
      - 'vivo'  (passa no filtro): estado_choch in (NOVO, RETESTADO)
      - 'morto' (bloqueado): estado_choch in (CONSUMIDO, INVALIDADO)
      - 'caso_invalido': estado_choch ausente ou fora do conjunto
        esperado — NUNCA assumido como morto, conforme instrução
        explícita do ticket. Registrado à parte para análise manual.
    """
    estado = setup.get('estado_choch')
    if estado in ('NOVO', 'RETESTADO'):
        return 'vivo'
    if estado in ('CONSUMIDO', 'INVALIDADO'):
        return 'morto'
    return 'caso_invalido'


def comparar_current_vs_filtro_choch_vivo(resultado_auditoria):
    """
    Reaproveita o resultado JÁ produzido por auditar_relevancia_choch()
    (setups_current, com estado_choch já calculado de forma causal) —
    NÃO roda novo replay, NÃO recalcula nenhuma estrutura. Aplica o
    filtro experimental e monta a comparação CURRENT vs CURRENT+FILTRO.
    O setup original nunca é alterado — cada setup anotado é uma CÓPIA
    com campos extras (filtro_choch_vivo, passa_filtro_experimental,
    eliminado_pelo_filtro_experimental), preservando os dados originais
    intactos.
    """
    setups = resultado_auditoria.get('setups_current', [])
    setups_anotados = []
    vivos, mortos, invalidos = [], [], []

    for s in setups:
        classificacao = aplicar_filtro_choch_vivo(s)
        s_anotado = dict(s)  # cópia — nunca altera o setup original
        s_anotado['filtro_choch_vivo'] = classificacao
        s_anotado['passa_filtro_experimental'] = classificacao == 'vivo'
        s_anotado['eliminado_pelo_filtro_experimental'] = classificacao == 'morto'
        setups_anotados.append(s_anotado)
        if classificacao == 'vivo':
            vivos.append(s_anotado)
        elif classificacao == 'morto':
            mortos.append(s_anotado)
        else:
            invalidos.append(s_anotado)

    def dist_long_short(lista):
        long_n = sum(1 for s in lista if s.get('direcao') == 'alta')
        short_n = sum(1 for s in lista if s.get('direcao') == 'baixa')
        return {'LONG': long_n, 'SHORT': short_n, 'outro_ou_ausente': len(lista) - long_n - short_n}

    return {
        'pair': resultado_auditoria.get('pair'),
        'total_setups_current': len(setups),
        'total_vivos_passa_filtro': len(vivos),
        'total_mortos_eliminado_filtro': len(mortos),
        'total_casos_invalidos_nao_classificados': len(invalidos),
        'percentual_preservado_pelo_filtro': round(100 * len(vivos) / len(setups), 1) if setups else None,
        'distribuicao_long_short_current': dist_long_short(setups),
        'distribuicao_long_short_filtro_vivo': dist_long_short(vivos),
        'tp_sl_disponivel': False,
        'nota_tp_sl': (
            'TP/SL indisponível — avaliar_vortex_decision_layer() nunca gera entry/sl/tp '
            '(SL_VORTEX e RR_VORTEX permanecem UNKNOWN, bloqueio já vigente de etapas '
            'anteriores). Conforme instrução explícita do ticket, nenhum backtest novo foi '
            'criado só para preencher este item — este campo fica marcado indisponível.'
        ),
        'setups_current_anotados': setups_anotados,
        'setups_eliminados_pelo_filtro': mortos,
        'casos_invalidos': invalidos,
        'nota_metodologica': (
            'FILTRO EXPERIMENTAL, não é lógica de produção — reaproveita exclusivamente o '
            'campo estado_choch já calculado por auditar_relevancia_choch() (causal, sem '
            'lookahead, já testado e aprovado), sem recalcular nenhuma estrutura. Não '
            'substitui nenhum gate, não altera CHoCH/FVG/Premium-Discount/SL/TP/R:R/MSS/BOS, '
            'não é chamado por nenhum caminho de produção. VIVO = NOVO ou RETESTADO. MORTO = '
            'CONSUMIDO ou INVALIDADO. Ausência/estado desconhecido NUNCA é assumido como '
            'morto — cai em caso_invalido, separado para análise manual, conforme instrução '
            'explícita.'
        ),
    }


def testar_filtro_choch_vivo(pair, dias_historico=7):
    """
    Roda auditar_relevancia_choch() (SEM ALTERAÇÃO NENHUMA) e aplica o
    filtro experimental por cima do resultado já causal. 1 par.
    """
    resultado_auditoria = auditar_relevancia_choch(pair, dias_historico=dias_historico)
    if 'erro' in resultado_auditoria:
        return resultado_auditoria
    return comparar_current_vs_filtro_choch_vivo(resultado_auditoria)


def testar_filtro_choch_vivo_todos_pares(dias_historico=7, pares=None):
    """
    Roda testar_filtro_choch_vivo() (sem alteração) pra cada par da
    lista, e agrega os resultados. Erro num par não derruba os demais.
    """
    pares = pares or PARES_MONITORADOS_REPLAY
    resultados_por_pair = {}
    pares_com_erro = []

    for p in pares:
        try:
            r = testar_filtro_choch_vivo(p, dias_historico=dias_historico)
        except Exception as e:
            r = {'erro': str(e)}
        resultados_por_pair[p] = r
        if 'erro' in r:
            pares_com_erro.append(p)

    total_current = total_vivos = total_mortos = total_invalidos = 0
    long_current = short_current = long_vivo = short_vivo = 0

    for p, r in resultados_por_pair.items():
        if 'erro' in r:
            continue
        total_current += r['total_setups_current']
        total_vivos += r['total_vivos_passa_filtro']
        total_mortos += r['total_mortos_eliminado_filtro']
        total_invalidos += r['total_casos_invalidos_nao_classificados']
        long_current += r['distribuicao_long_short_current']['LONG']
        short_current += r['distribuicao_long_short_current']['SHORT']
        long_vivo += r['distribuicao_long_short_filtro_vivo']['LONG']
        short_vivo += r['distribuicao_long_short_filtro_vivo']['SHORT']

    return {
        'dias_historico': dias_historico, 'pares_testados': pares,
        'pares_com_erro': pares_com_erro, 'benchmark_principal': 'BTCUSD',
        'consolidado_global': {
            'total_setups_current': total_current,
            'total_vivos_passa_filtro': total_vivos,
            'total_mortos_eliminado_filtro': total_mortos,
            'total_casos_invalidos': total_invalidos,
            'percentual_preservado_pelo_filtro': round(100 * total_vivos / total_current, 1) if total_current else None,
            'distribuicao_long_short_current': {'LONG': long_current, 'SHORT': short_current},
            'distribuicao_long_short_filtro_vivo': {'LONG': long_vivo, 'SHORT': short_vivo},
        },
        'tp_sl_disponivel': False,
        'nota_tp_sl': (
            'TP/SL indisponível em todos os pares — mesmo motivo já documentado por par '
            '(SL_VORTEX/RR_VORTEX permanecem UNKNOWN). Nenhum backtest novo foi criado.'
        ),
        'nota_metodologica': (
            'Cada par processado de forma totalmente independente, chamando '
            'testar_filtro_choch_vivo() sem nenhuma alteração — mesma metodologia causal já '
            'aprovada. Erro num par não derruba os demais. Nenhuma regra foi aplicada à '
            'produção — filtro puramente experimental, isolado.'
        ),
        'resultados_por_pair': resultados_por_pair,
    }


def _executar_filtro_choch_vivo_job(db_file, job_id, pair, dias_historico):
    try:
        resultado = testar_filtro_choch_vivo(pair, dias_historico=dias_historico)
        status_final = 'erro' if (isinstance(resultado, dict) and 'erro' in resultado) else 'concluido'
        with sqlite3.connect(db_file) as conn:
            conn.execute('''
                UPDATE scalp_replay_jobs SET status=?, resultado_json=?, finished_at=? WHERE job_id=?
            ''', (status_final, json.dumps(resultado, ensure_ascii=False), int(time.time()), job_id))
            conn.commit()
    except Exception as e:
        try:
            with sqlite3.connect(db_file) as conn:
                conn.execute('''
                    UPDATE scalp_replay_jobs SET status='erro', erro=?, finished_at=? WHERE job_id=?
                ''', (str(e), int(time.time()), job_id))
                conn.commit()
        except Exception as e2:
            print(f"[scalp_engine replay_jobs] erro ao registrar falha do job {job_id}: {e2}")


def _executar_filtro_choch_vivo_todos_pares_job(db_file, job_id, dias_historico, pares):
    try:
        resultado = testar_filtro_choch_vivo_todos_pares(dias_historico=dias_historico, pares=pares)
        with sqlite3.connect(db_file) as conn:
            conn.execute('''
                UPDATE scalp_replay_jobs SET status='concluido', resultado_json=?, finished_at=? WHERE job_id=?
            ''', (json.dumps(resultado, ensure_ascii=False), int(time.time()), job_id))
            conn.commit()
    except Exception as e:
        try:
            with sqlite3.connect(db_file) as conn:
                conn.execute('''
                    UPDATE scalp_replay_jobs SET status='erro', erro=?, finished_at=? WHERE job_id=?
                ''', (str(e), int(time.time()), job_id))
                conn.commit()
        except Exception as e2:
            print(f"[scalp_engine replay_jobs] erro ao registrar falha do job {job_id}: {e2}")


@explicacao_bp.route("/scalp_gates_vortex/testar_filtro_choch_vivo_iniciar", methods=["GET"])
def testar_filtro_choch_vivo_iniciar_endpoint():
    """
    Roda testar_filtro_choch_vivo() em BACKGROUND, 1 par. Consultar no
    MESMO endpoint de status genérico já existente.
    Uso: ?pair=BTCUSD&dias=7&confirm=RODAR_FILTRO_CHOCH_VIVO
    """
    if request.args.get('confirm') != 'RODAR_FILTRO_CHOCH_VIVO':
        return jsonify({
            "erro": "endpoint pesado, protegido contra chamada acidental",
            "como_usar": "adiciona &confirm=RODAR_FILTRO_CHOCH_VIVO na URL, ex: "
                          "/scalp_gates_vortex/testar_filtro_choch_vivo_iniciar?pair=BTCUSD&dias=7&confirm=RODAR_FILTRO_CHOCH_VIVO",
        }), 400

    pair = request.args.get('pair', 'BTCUSD')
    dias = int(request.args.get('dias', 7))
    db_file = _db_file_explicacao()
    init_replay_jobs_db(db_file)
    job_id = f"filtrochochvivo_{pair}_{int(time.time()*1000)}"

    try:
        with sqlite3.connect(db_file) as conn:
            conn.execute('''
                INSERT INTO scalp_replay_jobs (job_id, tipo, pair, dias_historico, status, created_at)
                VALUES (?, ?, ?, ?, 'rodando', ?)
            ''', (job_id, 'testar_filtro_choch_vivo', pair, dias, int(time.time())))
            conn.commit()
    except Exception as e:
        return jsonify({"erro": f"não foi possível criar o job: {e}"}), 500

    thread = threading.Thread(target=_executar_filtro_choch_vivo_job, args=(db_file, job_id, pair, dias), daemon=True)
    thread.start()

    return jsonify({
        "job_id": job_id, "status": "iniciado", "pair": pair, "dias_historico": dias,
        "como_consultar": f"/scalp_gates_vortex/replay_comparativo_luxalgo_status/{job_id}",
        "aviso": "roda em background — pode levar alguns minutos",
    })


@explicacao_bp.route("/scalp_gates_vortex/testar_filtro_choch_vivo_todos_pares_iniciar", methods=["GET"])
def testar_filtro_choch_vivo_todos_pares_iniciar_endpoint():
    """
    Roda testar_filtro_choch_vivo_todos_pares() em BACKGROUND, 13
    pares. Consultar no MESMO endpoint de status genérico já existente.
    Uso: ?dias=7&confirm=RODAR_FILTRO_CHOCH_VIVO_TODOS_PARES
    Opcional &pares=BTCUSD,ETHUSD,...
    """
    if request.args.get('confirm') != 'RODAR_FILTRO_CHOCH_VIVO_TODOS_PARES':
        return jsonify({
            "erro": "endpoint MUITO pesado, protegido contra chamada acidental",
            "como_usar": "adiciona &confirm=RODAR_FILTRO_CHOCH_VIVO_TODOS_PARES na URL, ex: "
                          "/scalp_gates_vortex/testar_filtro_choch_vivo_todos_pares_iniciar?dias=7&confirm=RODAR_FILTRO_CHOCH_VIVO_TODOS_PARES",
        }), 400

    dias = int(request.args.get('dias', 7))
    pares_param = request.args.get('pares')
    pares_lista = None
    if pares_param:
        pares_lista = [p.strip().upper() for p in pares_param.split(',') if p.strip()]

    db_file = _db_file_explicacao()
    init_replay_jobs_db(db_file)
    job_id = f"filtrochochvivotodos_{int(time.time()*1000)}"

    try:
        with sqlite3.connect(db_file) as conn:
            conn.execute('''
                INSERT INTO scalp_replay_jobs (job_id, tipo, pair, dias_historico, status, created_at)
                VALUES (?, ?, ?, ?, 'rodando', ?)
            ''', (job_id, 'testar_filtro_choch_vivo_todos_pares', 'TODOS', dias, int(time.time())))
            conn.commit()
    except Exception as e:
        return jsonify({"erro": f"não foi possível criar o job: {e}"}), 500

    thread = threading.Thread(
        target=_executar_filtro_choch_vivo_todos_pares_job,
        args=(db_file, job_id, dias, pares_lista), daemon=True,
    )
    thread.start()

    return jsonify({
        "job_id": job_id, "status": "iniciado", "dias_historico": dias,
        "pares": pares_lista or PARES_MONITORADOS_REPLAY,
        "como_consultar": f"/scalp_gates_vortex/replay_comparativo_luxalgo_status/{job_id}",
        "aviso": "MUITO pesado — roda em background sem prazo de conexão, mas pode levar bastante tempo",
    })


# ═══════════════════════════════════════════════════════════════════════
# FILTRO EXPERIMENTAL — APENAS INVALIDADO BLOQUEIA — item aprovado do
# ticket. SOMENTE TESTE/REPLAY/PAPER, sem deploy, sem alterar produção.
# NÃO recalcula estrutura nenhuma — reaproveita exclusivamente o campo
# estado_choch já calculado por auditar_relevancia_choch() e o
# resultado já produzido por comparar_current_vs_filtro_choch_vivo()
# (VIVO_MORTO), sem recalcular nenhum dos dois. Não substitui nenhum
# gate, não altera CHoCH/BOS/MSS/FVG/Premium-Discount/SL/TP/R:R/
# FVG↔CHoCH. Não é chamado por nenhum caminho de produção.
# ═══════════════════════════════════════════════════════════════════════

def aplicar_filtro_choch_invalidado(setup):
    """
    Classifica um setup JÁ PRODUZIDO por auditar_relevancia_choch()
    (reaproveita setup['estado_choch'], não recalcula nada) em:
      - 'passa': estado_choch in (NOVO, RETESTADO, CONSUMIDO)
      - 'bloqueado': estado_choch == INVALIDADO
      - 'caso_invalido': estado_choch ausente ou fora do conjunto
        esperado — NUNCA assumido como morto, conforme instrução
        explícita do ticket.
    Diferença única em relação a aplicar_filtro_choch_vivo(): CONSUMIDO
    passa aqui (não é bloqueado). Só INVALIDADO bloqueia.
    """
    estado = setup.get('estado_choch')
    if estado == 'INVALIDADO':
        return 'bloqueado'
    if estado in ('NOVO', 'RETESTADO', 'CONSUMIDO'):
        return 'passa'
    return 'caso_invalido'


def comparar_tres_filtros_choch(resultado_auditoria):
    """
    Reaproveita o resultado JÁ produzido por auditar_relevancia_choch()
    e por comparar_current_vs_filtro_choch_vivo() (VIVO_MORTO) — NÃO
    recalcula nenhuma estrutura, NÃO roda replay novo. Monta a
    comparação de 3 vias: CURRENT vs APENAS_INVALIDADO vs VIVO_MORTO.
    Nenhum setup original é alterado — cada setup anotado é cópia.
    """
    setups = resultado_auditoria.get('setups_current', [])

    def dist_ls(lista):
        l = sum(1 for s in lista if s.get('direcao') == 'alta')
        sh = sum(1 for s in lista if s.get('direcao') == 'baixa')
        return {'LONG': l, 'SHORT': sh, 'outro_ou_ausente': len(lista) - l - sh}

    passa, bloqueado, invalidos = [], [], []
    setups_anotados_ai = []
    for s in setups:
        c = aplicar_filtro_choch_invalidado(s)
        s2 = dict(s)  # cópia — nunca altera o setup original
        s2['filtro_apenas_invalidado'] = c
        setups_anotados_ai.append(s2)
        if c == 'passa':
            passa.append(s2)
        elif c == 'bloqueado':
            bloqueado.append(s2)
        else:
            invalidos.append(s2)

    apenas_invalidado_bloco = {
        'total_setups': len(setups),
        'total_preservado': len(passa),
        'total_eliminado': len(bloqueado),
        'total_casos_invalidos': len(invalidos),
        'percentual_preservado': round(100 * len(passa) / len(setups), 1) if setups else None,
        'distribuicao_long_short_antes': dist_ls(setups),
        'distribuicao_long_short_depois': dist_ls(passa),
        'setups_eliminados': bloqueado,
        'casos_invalidos': invalidos,
    }

    vm_raw = comparar_current_vs_filtro_choch_vivo(resultado_auditoria)
    vivo_morto_bloco = {
        'total_setups': vm_raw['total_setups_current'],
        'total_preservado': vm_raw['total_vivos_passa_filtro'],
        'total_eliminado': vm_raw['total_mortos_eliminado_filtro'],
        'total_casos_invalidos': vm_raw['total_casos_invalidos_nao_classificados'],
        'percentual_preservado': vm_raw['percentual_preservado_pelo_filtro'],
        'distribuicao_long_short_antes': vm_raw['distribuicao_long_short_current'],
        'distribuicao_long_short_depois': vm_raw['distribuicao_long_short_filtro_vivo'],
    }

    current_bloco = {'total_setups': len(setups), 'distribuicao_long_short': dist_ls(setups)}

    return {
        'pair': resultado_auditoria.get('pair'),
        'CURRENT': current_bloco,
        'APENAS_INVALIDADO': apenas_invalidado_bloco,
        'VIVO_MORTO': vivo_morto_bloco,
        'tp_sl_disponivel': False,
        'nota_tp_sl': (
            'TP/SL indisponível — avaliar_vortex_decision_layer() nunca gera entry/sl/tp '
            '(SL_VORTEX/RR_VORTEX permanecem UNKNOWN, bloqueio já vigente). Nenhum backtest '
            'novo foi criado só para preencher este item.'
        ),
        'setups_current_anotados_apenas_invalidado': setups_anotados_ai,
        'nota_metodologica': (
            'FILTRO EXPERIMENTAL — reaproveita exclusivamente estado_choch já calculado por '
            'auditar_relevancia_choch() (causal, sem lookahead, já testado) e o resultado já '
            'produzido por comparar_current_vs_filtro_choch_vivo(), sem recalcular nenhuma '
            'estrutura. Não substitui gate nenhum, não altera CHoCH/FVG/Premium-Discount/SL/'
            'TP/R:R/MSS/BOS, não é chamado por nenhum caminho de produção. APENAS_INVALIDADO: '
            'só INVALIDADO bloqueia — CONSUMIDO passa (diferença única vs VIVO_MORTO). Estado '
            'ausente/desconhecido NUNCA é assumido como morto.'
        ),
    }


def testar_tres_filtros_choch(pair, dias_historico=7):
    """Roda auditar_relevancia_choch() (SEM ALTERAÇÃO) e aplica os 3
    filtros comparativos por cima. 1 par."""
    resultado_auditoria = auditar_relevancia_choch(pair, dias_historico=dias_historico)
    if 'erro' in resultado_auditoria:
        return resultado_auditoria
    return comparar_tres_filtros_choch(resultado_auditoria)


def testar_tres_filtros_choch_todos_pares(dias_historico=7, pares=None):
    """Roda testar_tres_filtros_choch() (sem alteração) pra cada par,
    agregando os resultados. Erro num par não derruba os demais."""
    pares = pares or PARES_MONITORADOS_REPLAY
    resultados_por_pair = {}
    pares_com_erro = []

    for p in pares:
        try:
            r = testar_tres_filtros_choch(p, dias_historico=dias_historico)
        except Exception as e:
            r = {'erro': str(e)}
        resultados_por_pair[p] = r
        if 'erro' in r:
            pares_com_erro.append(p)

    def init_acc():
        return {
            'total_setups': 0, 'total_preservado': 0, 'total_eliminado': 0,
            'long_antes': 0, 'short_antes': 0, 'long_depois': 0, 'short_depois': 0,
            'pares_zero_sinais': [],
        }

    acc = {'CURRENT': init_acc(), 'APENAS_INVALIDADO': init_acc(), 'VIVO_MORTO': init_acc()}

    for p, r in resultados_por_pair.items():
        if 'erro' in r:
            continue
        acc['CURRENT']['total_setups'] += r['CURRENT']['total_setups']
        acc['CURRENT']['long_antes'] += r['CURRENT']['distribuicao_long_short']['LONG']
        acc['CURRENT']['short_antes'] += r['CURRENT']['distribuicao_long_short']['SHORT']

        for chave in ('APENAS_INVALIDADO', 'VIVO_MORTO'):
            bloco = r[chave]
            acc[chave]['total_setups'] += bloco['total_setups']
            acc[chave]['total_preservado'] += bloco['total_preservado']
            acc[chave]['total_eliminado'] += bloco['total_eliminado']
            acc[chave]['long_antes'] += bloco['distribuicao_long_short_antes']['LONG']
            acc[chave]['short_antes'] += bloco['distribuicao_long_short_antes']['SHORT']
            acc[chave]['long_depois'] += bloco['distribuicao_long_short_depois']['LONG']
            acc[chave]['short_depois'] += bloco['distribuicao_long_short_depois']['SHORT']
            if bloco['total_preservado'] == 0:
                acc[chave]['pares_zero_sinais'].append(p)

    for chave in ('APENAS_INVALIDADO', 'VIVO_MORTO'):
        total = acc[chave]['total_setups']
        acc[chave]['percentual_preservado'] = round(100 * acc[chave]['total_preservado'] / total, 1) if total else None
        acc[chave]['long_eliminados'] = acc[chave]['long_antes'] - acc[chave]['long_depois']
        acc[chave]['short_eliminados'] = acc[chave]['short_antes'] - acc[chave]['short_depois']

    return {
        'dias_historico': dias_historico, 'pares_testados': pares, 'pares_com_erro': pares_com_erro,
        'benchmark_principal': 'BTCUSD',
        'consolidado_global': acc,
        'tp_sl_disponivel': False,
        'nota_tp_sl': 'TP/SL indisponível em todos os pares — mesmo motivo documentado por par.',
        'nota_metodologica': (
            'Cada par processado de forma totalmente independente, chamando '
            'testar_tres_filtros_choch() sem nenhuma alteração — mesma metodologia causal já '
            'aprovada. Erro num par não derruba os demais. Nenhuma regra foi aplicada à '
            'produção — comparação puramente experimental, isolada.'
        ),
        'resultados_por_pair': resultados_por_pair,
    }


def _executar_tres_filtros_choch_job(db_file, job_id, pair, dias_historico):
    try:
        resultado = testar_tres_filtros_choch(pair, dias_historico=dias_historico)
        status_final = 'erro' if (isinstance(resultado, dict) and 'erro' in resultado) else 'concluido'
        with sqlite3.connect(db_file) as conn:
            conn.execute('''
                UPDATE scalp_replay_jobs SET status=?, resultado_json=?, finished_at=? WHERE job_id=?
            ''', (status_final, json.dumps(resultado, ensure_ascii=False), int(time.time()), job_id))
            conn.commit()
    except Exception as e:
        try:
            with sqlite3.connect(db_file) as conn:
                conn.execute('''
                    UPDATE scalp_replay_jobs SET status='erro', erro=?, finished_at=? WHERE job_id=?
                ''', (str(e), int(time.time()), job_id))
                conn.commit()
        except Exception as e2:
            print(f"[scalp_engine replay_jobs] erro ao registrar falha do job {job_id}: {e2}")


def _executar_tres_filtros_choch_todos_pares_job(db_file, job_id, dias_historico, pares):
    try:
        resultado = testar_tres_filtros_choch_todos_pares(dias_historico=dias_historico, pares=pares)
        with sqlite3.connect(db_file) as conn:
            conn.execute('''
                UPDATE scalp_replay_jobs SET status='concluido', resultado_json=?, finished_at=? WHERE job_id=?
            ''', (json.dumps(resultado, ensure_ascii=False), int(time.time()), job_id))
            conn.commit()
    except Exception as e:
        try:
            with sqlite3.connect(db_file) as conn:
                conn.execute('''
                    UPDATE scalp_replay_jobs SET status='erro', erro=?, finished_at=? WHERE job_id=?
                ''', (str(e), int(time.time()), job_id))
                conn.commit()
        except Exception as e2:
            print(f"[scalp_engine replay_jobs] erro ao registrar falha do job {job_id}: {e2}")


@explicacao_bp.route("/scalp_gates_vortex/testar_tres_filtros_choch_iniciar", methods=["GET"])
def testar_tres_filtros_choch_iniciar_endpoint():
    """
    Roda testar_tres_filtros_choch() em BACKGROUND, 1 par (CURRENT vs
    APENAS_INVALIDADO vs VIVO_MORTO). Consultar no MESMO endpoint de
    status genérico já existente.
    Uso: ?pair=BTCUSD&dias=7&confirm=RODAR_TRES_FILTROS_CHOCH
    """
    if request.args.get('confirm') != 'RODAR_TRES_FILTROS_CHOCH':
        return jsonify({
            "erro": "endpoint pesado, protegido contra chamada acidental",
            "como_usar": "adiciona &confirm=RODAR_TRES_FILTROS_CHOCH na URL, ex: "
                          "/scalp_gates_vortex/testar_tres_filtros_choch_iniciar?pair=BTCUSD&dias=7&confirm=RODAR_TRES_FILTROS_CHOCH",
        }), 400

    pair = request.args.get('pair', 'BTCUSD')
    dias = int(request.args.get('dias', 7))
    db_file = _db_file_explicacao()
    init_replay_jobs_db(db_file)
    job_id = f"tresfiltroschoch_{pair}_{int(time.time()*1000)}"

    try:
        with sqlite3.connect(db_file) as conn:
            conn.execute('''
                INSERT INTO scalp_replay_jobs (job_id, tipo, pair, dias_historico, status, created_at)
                VALUES (?, ?, ?, ?, 'rodando', ?)
            ''', (job_id, 'testar_tres_filtros_choch', pair, dias, int(time.time())))
            conn.commit()
    except Exception as e:
        return jsonify({"erro": f"não foi possível criar o job: {e}"}), 500

    thread = threading.Thread(target=_executar_tres_filtros_choch_job, args=(db_file, job_id, pair, dias), daemon=True)
    thread.start()

    return jsonify({
        "job_id": job_id, "status": "iniciado", "pair": pair, "dias_historico": dias,
        "como_consultar": f"/scalp_gates_vortex/replay_comparativo_luxalgo_status/{job_id}",
        "aviso": "roda em background — pode levar alguns minutos",
    })


@explicacao_bp.route("/scalp_gates_vortex/testar_tres_filtros_choch_todos_pares_iniciar", methods=["GET"])
def testar_tres_filtros_choch_todos_pares_iniciar_endpoint():
    """
    Roda testar_tres_filtros_choch_todos_pares() em BACKGROUND, 13
    pares. Consultar no MESMO endpoint de status genérico já existente.
    Uso: ?dias=7&confirm=RODAR_TRES_FILTROS_CHOCH_TODOS_PARES
    Opcional &pares=BTCUSD,ETHUSD,...
    """
    if request.args.get('confirm') != 'RODAR_TRES_FILTROS_CHOCH_TODOS_PARES':
        return jsonify({
            "erro": "endpoint MUITO pesado, protegido contra chamada acidental",
            "como_usar": "adiciona &confirm=RODAR_TRES_FILTROS_CHOCH_TODOS_PARES na URL, ex: "
                          "/scalp_gates_vortex/testar_tres_filtros_choch_todos_pares_iniciar?dias=7&confirm=RODAR_TRES_FILTROS_CHOCH_TODOS_PARES",
        }), 400

    dias = int(request.args.get('dias', 7))
    pares_param = request.args.get('pares')
    pares_lista = None
    if pares_param:
        pares_lista = [p.strip().upper() for p in pares_param.split(',') if p.strip()]

    db_file = _db_file_explicacao()
    init_replay_jobs_db(db_file)
    job_id = f"tresfiltroschochtodos_{int(time.time()*1000)}"

    try:
        with sqlite3.connect(db_file) as conn:
            conn.execute('''
                INSERT INTO scalp_replay_jobs (job_id, tipo, pair, dias_historico, status, created_at)
                VALUES (?, ?, ?, ?, 'rodando', ?)
            ''', (job_id, 'testar_tres_filtros_choch_todos_pares', 'TODOS', dias, int(time.time())))
            conn.commit()
    except Exception as e:
        return jsonify({"erro": f"não foi possível criar o job: {e}"}), 500

    thread = threading.Thread(
        target=_executar_tres_filtros_choch_todos_pares_job,
        args=(db_file, job_id, dias, pares_lista), daemon=True,
    )
    thread.start()

    return jsonify({
        "job_id": job_id, "status": "iniciado", "dias_historico": dias,
        "pares": pares_lista or PARES_MONITORADOS_REPLAY,
        "como_consultar": f"/scalp_gates_vortex/replay_comparativo_luxalgo_status/{job_id}",
        "aviso": "MUITO pesado — roda em background sem prazo de conexão, mas pode levar bastante tempo",
    })


# ═══════════════════════════════════════════════════════════════════════
# MFE/MAE CAUSAL — CURRENT vs APENAS_INVALIDADO — item aprovado do
# ticket (OPÇÃO A). SOMENTE AUDITORIA, sem SL/TP, sem WIN/LOSS, sem
# regra de entrada/saída nova. Reaproveita a MESMA convenção de MFE/MAE
# já usada em _avaliar_qualidade_sfp_evento() (LONG: mfe=max(high)-
# entry, mae=entry-min(low); SHORT: invertido) — só parametrizada com
# as janelas 20/50 pedidas no ticket, em vez das janelas fixas
# HORIZONTES_CANDLES já usadas por aquela função (que não é alterada).
# "entry" aqui é o CLOSE do candle onde o setup foi observado —
# referência causal pra medir excursão de preço, NÃO é uma ordem de
# entrada nem SL/TP. Não é lógica de produção, não é chamada por
# nenhum caminho de produção.
# ═══════════════════════════════════════════════════════════════════════

JANELAS_MFE_MAE_PADRAO = (20, 50)


def _medir_mfe_mae_janela(candles_futuros, direcao, entry, janela):
    """
    Mede MFE/MAE numa única janela, reaproveitando exatamente a mesma
    fórmula já usada em _avaliar_qualidade_sfp_evento() (não duplicada
    por reimplementação diferente, só reescrita isolada pra aceitar
    qualquer janela, não só as fixas de HORIZONTES_CANDLES). Se houver
    menos candles disponíveis que a janela pedida, NÃO inventa dado —
    devolve None e reporta quantos candles realmente existiam
    (mesmo padrão de 'horizontes_resultado[h] = None' já usado na
    função original).
    """
    candles_janela = candles_futuros[:janela]
    disponiveis = len(candles_janela)
    if disponiveis < janela:
        return {'mfe_pct': None, 'mae_pct': None, 'candles_disponiveis': disponiveis, 'janela_completa': False}

    if direcao == 'alta':
        mfe = max(0, max(c['h'] for c in candles_janela) - entry)
        mae = max(0, entry - min(c['l'] for c in candles_janela))
    else:  # 'baixa'
        mfe = max(0, entry - min(c['l'] for c in candles_janela))
        mae = max(0, max(c['h'] for c in candles_janela) - entry)

    return {
        'mfe_pct': round(mfe / entry * 100, 4) if entry else None,
        'mae_pct': round(mae / entry * 100, 4) if entry else None,
        'candles_disponiveis': disponiveis, 'janela_completa': True,
    }


def _percentil_lista(valores, p):
    """Reaproveita a mesma lógica de _percentil() já existente
    (percentil por índice sobre lista ordenada), só aceita a lista
    não-ordenada e ordena aqui — evita depender da ordem externa."""
    if not valores:
        return None
    return _percentil(sorted(valores), p)


def _agregar_mfe_mae(lista_medicoes, janelas):
    """
    Agrega MFE/MAE de uma lista de medições (já calculadas por
    _medir_mfe_mae_janela em cada setup), pra cada janela pedida.
    Só inclui na estatística os casos com janela_completa=True (mesmo
    padrão já usado pelo código existente — None não entra na conta),
    mas reporta explicitamente quantos foram excluídos por candles
    insuficientes, sem nunca excluir silenciosamente.
    """
    resultado = {}
    for j in janelas:
        mfe_validos = [m[f'j{j}']['mfe_pct'] for m in lista_medicoes if m[f'j{j}']['janela_completa']]
        mae_validos = [m[f'j{j}']['mae_pct'] for m in lista_medicoes if m[f'j{j}']['janela_completa']]
        n_insuficientes = sum(1 for m in lista_medicoes if not m[f'j{j}']['janela_completa'])

        def stats(lst):
            if not lst:
                return None
            return {
                'media': round(sum(lst) / len(lst), 4),
                'mediana': _percentil_lista(lst, 50),
                'p75': _percentil_lista(lst, 75),
                'p90': _percentil_lista(lst, 90),
                'n': len(lst),
            }

        resultado[f'janela_{j}'] = {
            'mfe': stats(mfe_validos), 'mae': stats(mae_validos),
            'total_medicoes': len(lista_medicoes),
            'candles_insuficientes': n_insuficientes,
            'incluidos_na_estatistica': len(mfe_validos),
        }
    return resultado


def medir_mfe_mae_choch_filtros(pair, dias_historico=7, janelas=JANELAS_MFE_MAE_PADRAO):
    """
    ÚNICA EXECUÇÃO — chama auditar_relevancia_choch() UMA vez (sem
    alteração de lógica nenhuma, só reaproveita o m5_completo agora
    exposto no retorno), deriva CURRENT e APENAS_INVALIDADO do MESMO
    conjunto de setups/candles, e mede MFE/MAE causal pra cada um
    (entry = close do candle onde o setup foi observado). Sem
    lookahead: candles_futuros = m5[idx_ciclo+1:], nunca usa candle
    anterior ou igual ao próprio evento.
    """
    resultado_auditoria = auditar_relevancia_choch(pair, dias_historico=dias_historico)
    if 'erro' in resultado_auditoria:
        return resultado_auditoria

    m5 = resultado_auditoria.get('m5_completo')
    setups = resultado_auditoria.get('setups_current', [])
    if not m5 or not setups:
        return {'erro': f'sem m5_completo ou setups_current pra {pair} — auditoria não produziu dado suficiente'}

    medicoes = []
    for s in setups:
        idx = s['idx_ciclo']
        direcao = s['direcao']
        entry_proxy = m5[idx]['c']  # preço no candle do próprio setup — referência causal, NÃO é SL/TP
        candles_futuros = m5[idx + 1:]  # sem lookahead — só candles estritamente depois do evento

        classif = aplicar_filtro_choch_invalidado(s)  # reaproveita função já testada, não recalcula nada

        med = {
            'pair': pair, 'idx_ciclo': idx, 'direcao': direcao, 'estado_choch': s['estado_choch'],
            'filtro_apenas_invalidado': classif, 'entry_proxy': entry_proxy,
        }
        for j in janelas:
            med[f'j{j}'] = _medir_mfe_mae_janela(candles_futuros, direcao, entry_proxy, j)
        medicoes.append(med)

    # ── Grupos: CURRENT (todos), PRESERVADOS (passa), ELIMINADOS (bloqueado) ──
    preservados = [m for m in medicoes if m['filtro_apenas_invalidado'] == 'passa']
    eliminados = [m for m in medicoes if m['filtro_apenas_invalidado'] == 'bloqueado']
    casos_invalidos = [m for m in medicoes if m['filtro_apenas_invalidado'] == 'caso_invalido']

    def bloco_grupo(lista):
        long_l = [m for m in lista if m['direcao'] == 'alta']
        short_l = [m for m in lista if m['direcao'] == 'baixa']
        return {
            'n_total': len(lista),
            'global': _agregar_mfe_mae(lista, janelas),
            'LONG': {'n': len(long_l), **_agregar_mfe_mae(long_l, janelas)} if long_l else {'n': 0},
            'SHORT': {'n': len(short_l), **_agregar_mfe_mae(short_l, janelas)} if short_l else {'n': 0},
        }

    return {
        'pair': pair, 'dias_historico': dias_historico, 'janelas_analisadas': list(janelas),
        'total_setups_current': len(setups),
        'total_preservados_apenas_invalidado': len(preservados),
        'total_eliminados_apenas_invalidado': len(eliminados),
        'total_casos_invalidos': len(casos_invalidos),
        'CURRENT': bloco_grupo(medicoes),
        'PRESERVADOS': bloco_grupo(preservados),
        'ELIMINADOS': bloco_grupo(eliminados),
        'confirmacoes': {
            'mesma_execucao_confirmado': True,
            'nota_mesma_execucao': (
                'CURRENT, PRESERVADOS e ELIMINADOS são todos derivados do MESMO m5_completo e '
                'do MESMO setups_current, obtidos numa única chamada a auditar_relevancia_choch() '
                'nesta execução — não há comparação entre execuções diferentes.'
            ),
            'sem_lookahead': True,
            'nota_lookahead': (
                'entry_proxy = close do candle em idx_ciclo (o próprio candle do setup). '
                'candles_futuros = m5[idx_ciclo+1:] — estritamente posteriores, nunca inclui o '
                'candle do evento nem qualquer candle anterior/igual a ele.'
            ),
            'exclusao_nao_silenciosa': True,
            'nota_exclusao': (
                'Casos com menos candles que a janela pedida (20 ou 50) recebem mfe_pct/mae_pct '
                '=None e são CONTADOS explicitamente em candles_insuficientes de cada janela — '
                'nunca descartados sem registro.'
            ),
        },
        'nota_metodologica': (
            'AUDITORIA/REPLAY SOMENTE — mede MFE/MAE (excursão de preço), NÃO WIN/LOSS, NÃO '
            'SL/TP, NÃO R:R. Reaproveita a mesma convenção de fórmula já usada em '
            '_avaliar_qualidade_sfp_evento() (LONG: mfe=max(high)-entry, mae=entry-min(low); '
            'SHORT: invertido), parametrizada pras janelas 20/50 pedidas. Não altera nenhum '
            'gate, CHoCH, MSS/BOS, FVG, Premium/Discount, SL, TP ou R:R existente. Não é '
            'chamada por nenhum caminho de produção.'
        ),
    }


def _executar_mfe_mae_choch_job(db_file, job_id, pair, dias_historico):
    try:
        resultado = medir_mfe_mae_choch_filtros(pair, dias_historico=dias_historico)
        status_final = 'erro' if (isinstance(resultado, dict) and 'erro' in resultado) else 'concluido'
        with sqlite3.connect(db_file) as conn:
            conn.execute('''
                UPDATE scalp_replay_jobs SET status=?, resultado_json=?, finished_at=? WHERE job_id=?
            ''', (status_final, json.dumps(resultado, ensure_ascii=False), int(time.time()), job_id))
            conn.commit()
    except Exception as e:
        try:
            with sqlite3.connect(db_file) as conn:
                conn.execute('''
                    UPDATE scalp_replay_jobs SET status='erro', erro=?, finished_at=? WHERE job_id=?
                ''', (str(e), int(time.time()), job_id))
                conn.commit()
        except Exception as e2:
            print(f"[scalp_engine replay_jobs] erro ao registrar falha do job {job_id}: {e2}")


@explicacao_bp.route("/scalp_gates_vortex/medir_mfe_mae_choch_iniciar", methods=["GET"])
def medir_mfe_mae_choch_iniciar_endpoint():
    """
    OPÇÃO A do ticket — mede MFE/MAE causal (janelas 20/50) comparando
    CURRENT vs PRESERVADOS vs ELIMINADOS pelo filtro APENAS_INVALIDADO,
    tudo numa ÚNICA execução (mesmo m5, mesmos setups). Roda em
    BACKGROUND. Consultar no MESMO endpoint de status genérico já
    existente. NÃO é SL/TP, NÃO é WIN/LOSS.
    Uso: ?pair=BTCUSD&dias=7&confirm=RODAR_MFE_MAE_CHOCH
    """
    if request.args.get('confirm') != 'RODAR_MFE_MAE_CHOCH':
        return jsonify({
            "erro": "endpoint pesado, protegido contra chamada acidental",
            "como_usar": "adiciona &confirm=RODAR_MFE_MAE_CHOCH na URL, ex: "
                          "/scalp_gates_vortex/medir_mfe_mae_choch_iniciar?pair=BTCUSD&dias=7&confirm=RODAR_MFE_MAE_CHOCH",
        }), 400

    pair = request.args.get('pair', 'BTCUSD')
    dias = int(request.args.get('dias', 7))
    db_file = _db_file_explicacao()
    init_replay_jobs_db(db_file)
    job_id = f"mfemaechoch_{pair}_{int(time.time()*1000)}"

    try:
        with sqlite3.connect(db_file) as conn:
            conn.execute('''
                INSERT INTO scalp_replay_jobs (job_id, tipo, pair, dias_historico, status, created_at)
                VALUES (?, ?, ?, ?, 'rodando', ?)
            ''', (job_id, 'medir_mfe_mae_choch_filtros', pair, dias, int(time.time())))
            conn.commit()
    except Exception as e:
        return jsonify({"erro": f"não foi possível criar o job: {e}"}), 500

    thread = threading.Thread(target=_executar_mfe_mae_choch_job, args=(db_file, job_id, pair, dias), daemon=True)
    thread.start()

    return jsonify({
        "job_id": job_id, "status": "iniciado", "pair": pair, "dias_historico": dias,
        "como_consultar": f"/scalp_gates_vortex/replay_comparativo_luxalgo_status/{job_id}",
        "aviso": "roda em background — pode levar alguns minutos",
    })
