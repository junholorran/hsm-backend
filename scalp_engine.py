# scalp_engine.py
# ─────────────────────────────────────────────────────────────────────────
# Motor de Scalp Ao Vivo — aditivo, não mexe em nada do cascade_engine.
# ─────────────────────────────────────────────────────────────────────────

import sqlite3
import hashlib
import time
import os
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


def find_active_zone(bandas, preco_atual):
    candidatas = [b for b in bandas if b['bottom'] <= preco_atual <= b['top']]
    if not candidatas:
        return None
    return max(candidatas, key=lambda b: b.get('ultimo_toque_ts', 0))


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


def _db_file_explicacao():
    return current_app.config.get('DB_FILE') or current_app.config.get('DB_PATH', '/data/alerts.db')


@explicacao_bp.route("/scalp/sinal/<signal_id>/explicacao", methods=["GET"])
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



def _fetch_bybit_klines_historico(symbol, interval, dias_historico, fim_ts_ms=None):
    """
    Busca candles históricos direto da Bybit V5, com paginação (a API
    limita a 1000 candles por request). Só usado pelo replay — nunca
    pelo pipeline de produção, que recebe candles já prontos de fora.

    fim_ts_ms — PARÂMETRO OPCIONAL, ADITIVO. Default None preserva
    100% o comportamento original (janela rolante a partir de "agora",
    idêntico a antes desta mudança — nenhum chamador existente é
    afetado). Quando fornecido, ancora o fim da janela nesse timestamp
    fixo, tornando o período reproduzível (não desloca com o tempo
    real entre execuções).
    """
    intervalo_ms = {'W': 604800000, 'D': 86400000, '240': 14400000, '60': 3600000, '30': 1800000, '15': 900000, '5': 300000, '1': 60000}.get(interval, 900000)
    total_candles_necessarios = int((dias_historico * 86400000) / intervalo_ms) + 20
    todos = []
    end_ts = fim_ts_ms

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


def _percentil(valores_ordenados, p):
    if not valores_ordenados:
        return None
    idx = int(len(valores_ordenados) * p / 100)
    idx = min(idx, len(valores_ordenados) - 1)
    return round(valores_ordenados[idx], 4)



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
    candles_since_first_sfp = round((event_ts - cluster_start) / intervalo_ms, 2)

    candles_since_previous_sfp = None
    if len(marcados) >= 2:
        candles_since_previous_sfp = round((event_ts - marcados[-2]['timestamp']) / intervalo_ms, 2)

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


def _kairos_confirmed_pivots(candles, left=KAIROS_SWEEP_LEFT, right=KAIROS_SWEEP_RIGHT):
    """Pivôs simétricos estilo Sweep Institutional, mas com causalidade explícita.
    O preço do pivô pertence ao candle origin_idx; ele só fica elegível em
    confirm_idx=origin_idx+right. Nunca devolvemos um pivot antes da confirmação.
    """
    out = []
    n = len(candles)
    if n < left + right + 1:
        return out
    for origin_idx in range(left, n - right):
        c = candles[origin_idx]
        lows_l = [x['l'] for x in candles[origin_idx-left:origin_idx]]
        lows_r = [x['l'] for x in candles[origin_idx+1:origin_idx+right+1]]
        highs_l = [x['h'] for x in candles[origin_idx-left:origin_idx]]
        highs_r = [x['h'] for x in candles[origin_idx+1:origin_idx+right+1]]
        confirm_idx = origin_idx + right
        if c['l'] <= min(lows_l + lows_r):
            out.append({'tipo':'low','nivel':c['l'],'origin_idx':origin_idx,'confirm_idx':confirm_idx,
                        'origin_ts':c['t'],'confirm_ts':candles[confirm_idx]['t']})
        if c['h'] >= max(highs_l + highs_r):
            out.append({'tipo':'high','nivel':c['h'],'origin_idx':origin_idx,'confirm_idx':confirm_idx,
                        'origin_ts':c['t'],'confirm_ts':candles[confirm_idx]['t']})
    out.sort(key=lambda x: (x['confirm_idx'], x['origin_idx']))
    return out


def _kairos_fvg_states(candles, lookback=250):
    """FVG/IFVG com ciclo de vida operacional.

    Estados operacionais: ATIVA -> TOCADA/PARCIAL. Wick profundo/full-fill
    não mata sozinho a FVG. Fechamento através da extremidade invalidadora
    confirma inversão e transforma a FVG em IFVG. IFVG pode ser INVALIDADA
    se depois fechar de volta através do lado oposto.
    """
    c=candles[-lookback:] if len(candles) > lookback else candles
    if len(c) < 3:
        return []
    states=[]
    for i in range(2, len(c)):
        a, meio, atual = c[i-2], c[i-1], c[i]
        novo=None
        if atual['l'] > a['h'] and meio['c'] > a['h']:
            novo={'id':f"FVG_{meio['t']}_B",'tipo':'FVG_bullish','direcao':'alta','top':atual['l'],'bottom':a['h'],
                  'created_ts':atual['t'],'origin_ts':meio['t'],'state':'ATIVA','flip_ts':None,
                  'first_touch_ts':None,'mitigated_ts':None,'invalidated_ts':None,
                  'source_a':dict(a),'source_mid':dict(meio),'source_c':dict(atual),'flip_candle':None}
        elif atual['h'] < a['l'] and meio['c'] < a['l']:
            novo={'id':f"FVG_{meio['t']}_S",'tipo':'FVG_bearish','direcao':'baixa','top':a['l'],'bottom':atual['h'],
                  'created_ts':atual['t'],'origin_ts':meio['t'],'state':'ATIVA','flip_ts':None,
                  'first_touch_ts':None,'mitigated_ts':None,'invalidated_ts':None,
                  'source_a':dict(a),'source_mid':dict(meio),'source_c':dict(atual),'flip_candle':None}
        if novo:
            states.append(novo)

        for z in states:
            if z['created_ts'] >= atual['t']:
                continue
            # FVG original: wick/touch NÃO mata a zona. O estado progride
            # por profundidade de preenchimento; somente um CLOSE através da
            # extremidade invalidadora confirma inversão e cria IFVG.
            if z['tipo'] == 'FVG_bullish':
                if atual['c'] < z['bottom']:
                    z['state']='IFVG'; z['tipo']='IFVG_bearish'; z['direcao']='baixa'; z['flip_ts']=atual['t']; z['flip_candle']=dict(atual)
                elif atual['l'] <= z['bottom']:
                    z['state']='PARCIAL'; z['first_touch_ts']=z['first_touch_ts'] or atual['t']
                    z['mitigated_ts']=z['mitigated_ts'] or atual['t']
                elif atual['l'] < z['top']:
                    z['state']='PARCIAL' if atual['l'] < (z['top']+z['bottom'])/2 else 'TOCADA'
                    z['first_touch_ts']=z['first_touch_ts'] or atual['t']
            elif z['tipo'] == 'FVG_bearish':
                if atual['c'] > z['top']:
                    z['state']='IFVG'; z['tipo']='IFVG_bullish'; z['direcao']='alta'; z['flip_ts']=atual['t']; z['flip_candle']=dict(atual)
                elif atual['h'] >= z['top']:
                    z['state']='PARCIAL'; z['first_touch_ts']=z['first_touch_ts'] or atual['t']
                    z['mitigated_ts']=z['mitigated_ts'] or atual['t']
                elif atual['h'] > z['bottom']:
                    z['state']='PARCIAL' if atual['h'] > (z['top']+z['bottom'])/2 else 'TOCADA'
                    z['first_touch_ts']=z['first_touch_ts'] or atual['t']
            elif z['tipo'] == 'IFVG_bearish' and z['state'] != 'INVALIDADA':
                if atual['c'] > z['top']:
                    z['state']='INVALIDADA'; z['invalidated_ts']=atual['t']
                elif atual['h'] >= z['bottom']:
                    z['first_touch_ts']=z['first_touch_ts'] or atual['t']
            elif z['tipo'] == 'IFVG_bullish' and z['state'] != 'INVALIDADA':
                if atual['c'] < z['bottom']:
                    z['state']='INVALIDADA'; z['invalidated_ts']=atual['t']
                elif atual['l'] <= z['top']:
                    z['first_touch_ts']=z['first_touch_ts'] or atual['t']
    return states

def _kairos_momentum_z(candles, period=50):
    if len(candles) < period + 1:
        return None
    changes=[candles[i]['c']-candles[i-1]['c'] for i in range(1,len(candles))]
    w=changes[-period:]
    avg=sum(w)/len(w)
    var=sum((x-avg)**2 for x in w)/len(w)
    std=var**0.5
    return 0.0 if std == 0 else (w[-1]-avg)/std


def _kairos_ob_from_break(candles, break_idx, direcao, search_back=10):
    """OB causal ligado à quebra: primeiro candle oposto nos 10 candles
    anteriores ao break; zona = high/low inteiro do candle, não body arbitrário.
    """
    if break_idx is None or break_idx <= 0:
        return None
    for j in range(break_idx-1, max(-1, break_idx-search_back-1), -1):
        c=candles[j]
        bearish = c['c'] < c['o']
        bullish = c['c'] > c['o']
        if (direcao=='alta' and bearish) or (direcao=='baixa' and bullish):
            return {'tipo':'OB_bullish' if direcao=='alta' else 'OB_bearish', 'direcao':direcao,
                    'top':c['h'],'bottom':c['l'],'t':c['t'],'idx':j,'break_idx':break_idx}
    return None


def _kairos_volume_context(candles, bins=24, lookback=200):
    """Volume/VP aproximado com OHLCV: distribui cada candle no seu preço típico.
    Não é order-flow por tick; serve como contexto/score, nunca trava obrigatória.
    """
    c=candles[-lookback:] if len(candles)>lookback else candles
    if not c:
        return None
    vols=[x.get('v',0) or 0 for x in c]
    avg=sum(vols)/len(vols) if vols else 0
    rel=(vols[-1]/avg) if avg else None
    lo=min(x['l'] for x in c); hi=max(x['h'] for x in c)
    if hi <= lo:
        return {'relative_volume':rel,'poc':c[-1]['c'],'hvn':[],'lvn':[]}
    step=(hi-lo)/bins
    hist=[0.0]*bins
    for x in c:
        typical=(x['h']+x['l']+x['c'])/3.0
        idx=min(bins-1,max(0,int((typical-lo)/step)))
        hist[idx]+=x.get('v',0) or 0
    poc_i=max(range(bins), key=lambda i: hist[i])
    centers=[lo+(i+0.5)*step for i in range(bins)]
    ranked=sorted(range(bins), key=lambda i: hist[i], reverse=True)
    lowrank=sorted(range(bins), key=lambda i: hist[i])
    return {'relative_volume':round(rel,3) if rel is not None else None,
            'poc':round(centers[poc_i],6),
            'hvn':[round(centers[i],6) for i in ranked[:3]],
            'lvn':[round(centers[i],6) for i in lowrank[:3]]}


def _kairos_liquidity_state(candles, tipo, nivel, confirm_ts):
    """Estado causal de um pool: ATIVA até o primeiro trade através do nível."""
    for c in candles:
        if c['t'] <= confirm_ts:
            continue
        if tipo in ('high','EQH') and c['h'] > nivel:
            return 'SWEPT', c['t']
        if tipo in ('low','EQL') and c['l'] < nivel:
            return 'SWEPT', c['t']
    return 'ATIVA', None


def _kairos_equal_liquidity_clusters(candles, pivots, atr):
    """EQH/EQL em clusters confirmados de 2+ pivôs, sem score."""
    if not atr:
        return []
    tol=0.1*atr
    out=[]
    for typ,name in (('high','EQH'),('low','EQL')):
        pts=[p for p in pivots if p['tipo']==typ]
        cluster=[]
        for p in pts:
            if not cluster:
                cluster=[p]; continue
            center=sum(x['nivel'] for x in cluster)/len(cluster)
            if abs(p['nivel']-center) <= tol:
                cluster.append(p)
            else:
                if len(cluster)>=2:
                    nivel=sum(x['nivel'] for x in cluster)/len(cluster)
                    cts=max(x['confirm_ts'] for x in cluster)
                    state,swept_ts=_kairos_liquidity_state(candles,name,nivel,cts)
                    out.append({'tipo':name,'nivel':nivel,'toques':len(cluster),'confirm_ts':cts,
                                'origin_ts':min(x['origin_ts'] for x in cluster),'state':state,'swept_ts':swept_ts})
                cluster=[p]
        if len(cluster)>=2:
            nivel=sum(x['nivel'] for x in cluster)/len(cluster)
            cts=max(x['confirm_ts'] for x in cluster)
            state,swept_ts=_kairos_liquidity_state(candles,name,nivel,cts)
            out.append({'tipo':name,'nivel':nivel,'toques':len(cluster),'confirm_ts':cts,
                        'origin_ts':min(x['origin_ts'] for x in cluster),'state':state,'swept_ts':swept_ts})
    out.sort(key=lambda x:x['confirm_ts'])
    return out



def _kairos_liquidity_pools(candles, pivots=None, atr=None, min_touches=2):
    """Pools operacionais de liquidez, não pivôs isolados.

    Um pool nasce quando 2+ pivôs CONFIRMADOS do mesmo lado ficam na mesma
    faixa de preço (tolerância = 0.10 * ATR200, matemática coerente com EQH/EQL
    do Lux SMC já usado no projeto). O pool guarda uma ZONA [bottom, top],
    número de toques, timestamps de origem/confirmação e estado causal.

    BUY_SIDE  = cluster de highs / EQH.
    SELL_SIDE = cluster de lows  / EQL.
    """
    if not candles:
        return []
    if pivots is None:
        pivots=_kairos_confirmed_pivots(candles)
    if atr is None:
        atrs=compute_atr(candles, 200)
        atr=next((v for v in reversed(atrs) if v is not None), None)
    if not atr or atr <= 0:
        return []
    tol=0.10*atr
    pools=[]
    for typ,side,label in (('high','BUY_SIDE','EQH'),('low','SELL_SIDE','EQL')):
        pts=sorted([p for p in pivots if p.get('tipo')==typ], key=lambda x:x['confirm_ts'])
        clusters=[]
        for p in pts:
            best=None; best_d=None
            for cl in clusters:
                center=sum(x['nivel'] for x in cl)/len(cl)
                d=abs(p['nivel']-center)
                if d <= tol and (best_d is None or d<best_d):
                    best=cl; best_d=d
            if best is None:
                clusters.append([p])
            else:
                best.append(p)
        for cl in clusters:
            if len(cl) < min_touches:
                continue
            levels=[x['nivel'] for x in cl]
            center=sum(levels)/len(levels)
            # pool é zona real dos toques; pequeno buffer evita tratar décimos de tick como outro pool
            bottom=min(levels)-tol*0.15
            top=max(levels)+tol*0.15
            confirm_ts=max(x['confirm_ts'] for x in cl)
            origin_ts=min(x['origin_ts'] for x in cl)
            swept_ts=None
            sweep_extreme=None
            for c in candles:
                if c['t'] <= confirm_ts:
                    continue
                if side=='BUY_SIDE' and c['h'] > top:
                    swept_ts=c['t']; sweep_extreme=c['h']; break
                if side=='SELL_SIDE' and c['l'] < bottom:
                    swept_ts=c['t']; sweep_extreme=c['l']; break
            pools.append({
                'id':f"{side}_{origin_ts}_{confirm_ts}", 'side':side, 'tipo':label,
                'nivel':center, 'bottom':bottom, 'top':top, 'toques':len(cl),
                'origin_ts':origin_ts, 'confirm_ts':confirm_ts,
                'touches':[{'nivel':x['nivel'],'origin_ts':x['origin_ts'],'confirm_ts':x['confirm_ts']} for x in cl],
                'state':'SWEPT' if swept_ts is not None else 'ATIVA',
                'swept_ts':swept_ts, 'sweep_extreme':sweep_extreme,
            })
    pools.sort(key=lambda x:(x['confirm_ts'],x['nivel']))
    return pools


def _kairos_order_blocks_map(candles, swing_size=20, lookback=300):
    """OBs persistentes ligados a BOS/CHoCH, usando a matemática já escolhida:
    quebra estrutural -> primeiro candle oposto nos 10 candles anteriores.
    Mantém somente metadados causais e estado operacional simples.
    """
    if not candles or len(candles)<20:
        return []
    cs=candles[-lookback:] if len(candles)>lookback else candles
    size=min(swing_size,max(5,len(cs)//6))
    events=compute_lux_structure_events(cs,swing_size=size)
    out=[]; seen=set()
    for e in events:
        ob=_kairos_ob_from_break(cs,e.get('index'),e.get('direcao'),search_back=10)
        if not ob:
            continue
        key=(ob['t'],ob['direcao'],round(ob['top'],10),round(ob['bottom'],10))
        if key in seen: continue
        seen.add(key)
        state='ATIVA'; invalidated_ts=None; first_touch_ts=None
        for c in cs:
            if c['t'] <= e['t']:
                continue
            if c['h']>=ob['bottom'] and c['l']<=ob['top'] and first_touch_ts is None:
                first_touch_ts=c['t']
            if ob['direcao']=='alta' and c['c'] < ob['bottom']:
                state='INVALIDADA'; invalidated_ts=c['t']; break
            if ob['direcao']=='baixa' and c['c'] > ob['top']:
                state='INVALIDADA'; invalidated_ts=c['t']; break
        out.append({**ob,'break_ts':e['t'],'break_tipo':e['tipo'],'state':state,
                    'first_touch_ts':first_touch_ts,'invalidated_ts':invalidated_ts})
    return out[-60:]


def _kairos_pool_poi_overlaps(pool, zones=None, order_blocks=None):
    """POIs que contêm/intersectam o pool. Isto é confluência geométrica real,
    não score. Retorna FVG/IFVG/OB ativos que efetivamente cruzam o pool.
    """
    hits=[]
    pb,pt=pool['bottom'],pool['top']
    for z in zones or []:
        if z.get('state') not in ('ATIVA','TOCADA','PARCIAL','IFVG'):
            continue
        if z.get('top') is None or z.get('bottom') is None: continue
        if z['top'] >= pb and z['bottom'] <= pt:
            hits.append({'tipo':z.get('tipo'),'top':z['top'],'bottom':z['bottom'],
                         'origin_ts':z.get('origin_ts'),'created_ts':z.get('created_ts'),'state':z.get('state')})
    for ob in order_blocks or []:
        if ob.get('state')!='ATIVA': continue
        if ob['top'] >= pb and ob['bottom'] <= pt:
            hits.append({'tipo':ob.get('tipo'),'top':ob['top'],'bottom':ob['bottom'],
                         'origin_ts':ob.get('t'),'created_ts':ob.get('break_ts'),'state':ob.get('state')})
    return hits


def _kairos_pool_sweeps(candles, pools):
    """Sweep/SFP contra POOLS confirmados (não contra swing isolado).
    Algoryze-style: atravessa o pool, recupera o nível médio no próprio candle
    e mantém 3 fechamentos do lado recuperado. Cada pool gera no máximo 1 evento.
    """
    out=[]
    for pool in pools:
        for i,c in enumerate(candles):
            if c['t'] <= pool['confirm_ts']:
                continue
            if pool['side']=='SELL_SIDE':
                raw=(c['l'] < pool['bottom'] and c['c'] > pool['nivel'] and c['o'] > pool['nivel'])
                direcao='alta'
            else:
                raw=(c['h'] > pool['top'] and c['c'] < pool['nivel'] and c['o'] < pool['nivel'])
                direcao='baixa'
            if not raw:
                continue
            confirmed=False; confirm_ts=None
            if i+3 < len(candles):
                if direcao=='alta':
                    confirmed=all(candles[j]['c'] > pool['nivel'] for j in (i+1,i+2,i+3))
                else:
                    confirmed=all(candles[j]['c'] < pool['nivel'] for j in (i+1,i+2,i+3))
                if confirmed: confirm_ts=candles[i+3]['t']
            out.append({'direcao':direcao,'side':pool['side'],'nivel':pool['nivel'],
                        'pool_bottom':pool['bottom'],'pool_top':pool['top'],'toques':pool['toques'],
                        'extremo':c['l'] if direcao=='alta' else c['h'],
                        'sweep_idx':i,'sweep_ts':c['t'],'confirmado_3b':confirmed,'confirm_ts':confirm_ts,
                        'liquidity_origin_ts':pool['origin_ts'],'liquidity_confirm_ts':pool['confirm_ts'],
                        'pool_id':pool['id'],'pool_poi':pool.get('poi_overlaps',[])})
            break
    out.sort(key=lambda x:x['sweep_ts'])
    return out

def _kairos_liquidity_map_tf(candles, tf):
    pivots=_kairos_confirmed_pivots(candles)
    atrs=compute_atr(candles, 200)
    atr=next((v for v in reversed(atrs) if v is not None), None)
    enriched=[]
    for p in pivots:
        q=dict(p)
        q['state'],q['swept_ts']=_kairos_liquidity_state(candles,p['tipo'],p['nivel'],p['confirm_ts'])
        enriched.append(q)
    eq=_kairos_equal_liquidity_clusters(candles,enriched,atr)
    fvg=_kairos_fvg_states(candles)
    obs=_kairos_order_blocks_map(candles)
    pools=_kairos_liquidity_pools(candles,pivots=enriched,atr=atr,min_touches=2)
    for p in pools:
        p['poi_overlaps']=_kairos_pool_poi_overlaps(p,fvg,obs)
    sweeps=_kairos_pool_sweeps(candles,pools)
    return {'tf':tf,'pivots':enriched[-40:],'equal_liquidity':eq[-30:],
            'liquidity_pools':pools[-40:],'sweeps':sweeps[-30:],
            'zones':fvg[-80:],'order_blocks':obs[-60:],'volume':_kairos_volume_context(candles)}


# ═══════════════════════════════════════════════════════════════════════
# BTC — AUDITORIA MATEMÁTICA INDEPENDENTE (SÓ LEITURA / SEM ALTERAR TRADES)
# Valida pivôs 20/20, pools 2+ toques, overlap POI, sweeps e causalidade.
# Não entra na decisão operacional, não envia Telegram e não grava DB.
# ═══════════════════════════════════════════════════════════════════════

KAIROS_BTC_AUDIT_TFS = {
    'H4': ('240', 45),
    'H1': ('60', 20),
    'M30': ('30', 12),
    'M15': ('15', 8),
    'M5': ('5', 4),
    'M1': ('1', 2),
}


def _kairos_audit_pivot_independente(candles, pivot, left=20, right=20):
    """Recalcula o pivô sem chamar _kairos_confirmed_pivots()."""
    oi = pivot.get('origin_idx')
    ci = pivot.get('confirm_idx')
    tipo = pivot.get('tipo')
    if oi is None or ci is None or oi < left or oi + right >= len(candles):
        return {'pass': False, 'reason': 'INDICES_INVALIDOS'}
    if ci != oi + right:
        return {'pass': False, 'reason': 'CONFIRM_IDX_INVALIDO', 'esperado': oi + right, 'recebido': ci}
    c = candles[oi]
    if pivot.get('origin_ts') != c['t'] or pivot.get('confirm_ts') != candles[ci]['t']:
        return {'pass': False, 'reason': 'TIMESTAMP_INVALIDO'}
    if pivot.get('nivel') != (c['h'] if tipo == 'high' else c['l']):
        return {'pass': False, 'reason': 'NIVEL_NAO_BATE_CANDLE_ORIGEM'}
    if tipo == 'high':
        esquerda = max(x['h'] for x in candles[oi-left:oi])
        direita = max(x['h'] for x in candles[oi+1:oi+right+1])
        ok = c['h'] >= esquerda and c['h'] >= direita
    elif tipo == 'low':
        esquerda = min(x['l'] for x in candles[oi-left:oi])
        direita = min(x['l'] for x in candles[oi+1:oi+right+1])
        ok = c['l'] <= esquerda and c['l'] <= direita
    else:
        return {'pass': False, 'reason': 'TIPO_INVALIDO'}
    return {
        'pass': bool(ok), 'reason': 'OK' if ok else 'FORMULA_20_20_FALHOU',
        'origin_idx': oi, 'confirm_idx': ci, 'origin_ts': c['t'], 'confirm_ts': candles[ci]['t'],
        'tipo': tipo, 'nivel': pivot.get('nivel'), 'left_extreme': esquerda, 'right_extreme': direita,
        'causal': candles[ci]['t'] > c['t'],
    }


def _kairos_audit_pool_independente(candles, pool, atr):
    """Confere exatamente os toques declarados, pivôs 20/20, tolerância e causalidade."""
    if not atr or atr <= 0:
        return {'pass': False, 'reason': 'SEM_ATR200'}
    tol = 0.1 * atr
    side = pool.get('side')
    wanted = 'high' if side == 'BUY_SIDE' else 'low'
    declared = pool.get('touches') or []
    if len(declared) < 2 or pool.get('toques') != len(declared):
        return {'pass': False, 'reason': 'TOQUES_DECLARADOS_INVALIDOS',
                'toques': pool.get('toques'), 'touches_len': len(declared)}

    # Índice por timestamps dos pivôs realmente confirmados pela fórmula 20/20.
    real = _kairos_confirmed_pivots(candles)
    by_key = {(p['origin_ts'], p['confirm_ts'], p['tipo']): p for p in real}
    checks=[]
    levels=[]
    for t in declared:
        rp = by_key.get((t.get('origin_ts'), t.get('confirm_ts'), wanted))
        if not rp:
            checks.append(False); continue
        same_level = abs(float(rp['nivel']) - float(t.get('nivel'))) <= 1e-12
        aud = _kairos_audit_pivot_independente(candles, rp)
        checks.append(bool(same_level and aud.get('pass') and aud.get('causal')))
        levels.append(float(rp['nivel']))

    center_expected = sum(levels)/len(levels) if levels else None
    center_ok = center_expected is not None and abs(center_expected - float(pool.get('nivel'))) <= 1e-9
    tolerance_ok = bool(levels) and all(abs(x - float(pool['nivel'])) <= tol for x in levels)
    bounds_expected_bottom = min(levels) - tol*0.15 if levels else None
    bounds_expected_top = max(levels) + tol*0.15 if levels else None
    bounds_ok = bool(levels) and abs(bounds_expected_bottom - float(pool.get('bottom'))) <= 1e-9 and abs(bounds_expected_top - float(pool.get('top'))) <= 1e-9
    confirm_expected = max(t['confirm_ts'] for t in declared) if declared else None
    origin_expected = min(t['origin_ts'] for t in declared) if declared else None
    timestamps_ok = pool.get('confirm_ts') == confirm_expected and pool.get('origin_ts') == origin_expected
    causal_ok = bool(declared) and all(t['confirm_ts'] <= pool['confirm_ts'] for t in declared)
    pass_all = all(checks) and center_ok and tolerance_ok and bounds_ok and timestamps_ok and causal_ok
    return {
        'pass': bool(pass_all), 'reason': 'OK' if pass_all else 'POOL_INVALIDO',
        'side': side, 'nivel': pool.get('nivel'), 'bottom': pool.get('bottom'), 'top': pool.get('top'),
        'toques': pool.get('toques'), 'tolerancia': tol, 'touches_reais': sum(checks),
        'center_ok': center_ok, 'tolerance_ok': tolerance_ok, 'bounds_ok': bounds_ok,
        'timestamps_ok': timestamps_ok, 'causal_ok': causal_ok,
    }


def _kairos_audit_sweep_independente(candles, sweep, pools):
    """Recalcula travessia + recuperação + confirmação 3 barras."""
    i = sweep.get('sweep_idx')
    if i is None or i < 0 or i >= len(candles):
        return {'pass': False, 'reason': 'SWEEP_IDX_INVALIDO'}
    pool = next((p for p in pools if p.get('id') == sweep.get('pool_id')), None)
    if not pool:
        return {'pass': False, 'reason': 'POOL_DO_SWEEP_NAO_ENCONTRADO'}
    if pool.get('confirm_ts', 0) >= candles[i]['t']:
        return {'pass': False, 'reason': 'SWEEP_ANTES_DA_CONFIRMACAO_POOL'}
    c = candles[i]
    if sweep.get('direcao') == 'alta':
        crossed = c['l'] < pool['bottom']
        recovered = c['c'] > pool['nivel']
        confirms = i + 3 < len(candles) and all(candles[j]['c'] > pool['nivel'] for j in (i+1, i+2, i+3))
    else:
        crossed = c['h'] > pool['top']
        recovered = c['c'] < pool['nivel']
        confirms = i + 3 < len(candles) and all(candles[j]['c'] < pool['nivel'] for j in (i+1, i+2, i+3))
    declared = bool(sweep.get('confirmado_3b'))
    pass_all = crossed and recovered and (declared == confirms)
    return {
        'pass': bool(pass_all), 'reason': 'OK' if pass_all else 'SWEEP_FORMULA_FALHOU',
        'pool_id': pool.get('id'), 'pool_side': pool.get('side'), 'pool_nivel': pool.get('nivel'),
        'sweep_ts': c['t'], 'crossed': crossed, 'recovered': recovered,
        'confirm_3bars_recalc': confirms, 'confirm_3bars_declared': declared,
        'causal': pool.get('confirm_ts', 0) < c['t'],
    }


def _kairos_auditar_tf_btc(candles, tf, sample_limit=100):
    mapa = _kairos_liquidity_map_tf(candles, tf)
    atrs = compute_atr(candles, 200)
    atr = next((v for v in reversed(atrs) if v is not None), None)
    pivots_all = _kairos_confirmed_pivots(candles)
    pools_all = _kairos_liquidity_pools(candles, pivots=pivots_all, atr=atr, min_touches=2)
    fvgs = _kairos_fvg_states(candles)
    obs = _kairos_order_blocks_map(candles)
    for p in pools_all:
        p['poi_overlaps'] = _kairos_pool_poi_overlaps(p, fvgs, obs)
    sweeps_all = _kairos_pool_sweeps(candles, pools_all)

    pivot_checks = [_kairos_audit_pivot_independente(candles, p) for p in pivots_all[-sample_limit:]]
    pool_checks = [_kairos_audit_pool_independente(candles, p, atr) for p in pools_all[-sample_limit:]]
    sweep_checks = [_kairos_audit_sweep_independente(candles, s, pools_all) for s in sweeps_all[-sample_limit:]]

    def resumo(xs):
        return {'total': len(xs), 'pass': sum(1 for x in xs if x.get('pass')), 'fail': sum(1 for x in xs if not x.get('pass'))}

    # Amostras úteis pro gráfico: últimos eventos, já com timestamps e preços.
    samples = {
        'pivots': pivot_checks[-10:],
        'pools': [dict(p, audit=_kairos_audit_pool_independente(candles, p, atr)) for p in pools_all[-10:]],
        'sweeps': [dict(s, audit=_kairos_audit_sweep_independente(candles, s, pools_all)) for s in sweeps_all[-10:]],
    }
    # Verifica overlap declarado POI geometricamente.
    overlap_fail = 0
    overlap_total = 0
    for p in pools_all[-sample_limit:]:
        for z in p.get('poi_overlaps', []):
            overlap_total += 1
            if not (p['top'] >= z.get('bottom', float('inf')) and p['bottom'] <= z.get('top', float('-inf'))):
                overlap_fail += 1

    return {
        'tf': tf, 'candles': len(candles), 'atr200': atr,
        'pivots': resumo(pivot_checks), 'pools': resumo(pool_checks), 'sweeps': resumo(sweep_checks),
        'poi_overlap': {'total': overlap_total, 'pass': overlap_total-overlap_fail, 'fail': overlap_fail},
        'all_math_pass': all(x.get('pass') for x in pivot_checks + pool_checks + sweep_checks) and overlap_fail == 0,
        'samples': samples,
    }


def auditar_btc_liquidez_matematica(sample_limit=100, fim_ts_ms=None):
    """Auditoria on-demand só de BTCUSD; não altera produção."""
    sample_limit = max(10, min(int(sample_limit or 100), 300))
    resultados = {}
    for tf, (interval, dias) in KAIROS_BTC_AUDIT_TFS.items():
        candles = _fetch_bybit_klines_historico('BTCUSD', interval, dias, fim_ts_ms=fim_ts_ms)
        resultados[tf] = _kairos_auditar_tf_btc(candles, tf, sample_limit=sample_limit)
    total_fail = sum(
        r['pivots']['fail'] + r['pools']['fail'] + r['sweeps']['fail'] + r['poi_overlap']['fail']
        for r in resultados.values()
    )
    return {
        'pair': 'BTCUSD', 'sample_limit_por_tf': sample_limit,
        'timeframes': list(KAIROS_BTC_AUDIT_TFS.keys()),
        'total_fail': total_fail, 'all_math_pass': total_fail == 0,
        'resultado': resultados,
        'nota': 'Auditoria independente de pivô 20/20, pool 2+ toques, overlap POI e sweep causal. Não altera sinal/SL/TP.'
    }


@explicacao_bp.route('/scalp_gates_vortex/auditoria_btc_liquidez', methods=['GET'])
def auditoria_btc_liquidez_endpoint():
    try:
        n = int(request.args.get('eventos', 100))
    except Exception:
        n = 100
    try:
        fim = request.args.get('fim_ts_ms')
        fim = int(fim) if fim else None
        r = auditar_btc_liquidez_matematica(sample_limit=n, fim_ts_ms=fim)
        return jsonify(r), 200
    except Exception as e:
        import traceback
        traceback.print_exc()
        return jsonify({'erro': str(e), 'pair': 'BTCUSD'}), 500


def _kairos_build_mtf_map(candles_por_tf):
    mapa={}
    for tf in KAIROS_TF_ORDEM:
        cs=candles_por_tf.get(tf) or []
        if cs:
            mapa[tf]=_kairos_liquidity_map_tf(cs, tf)
    return mapa



# ═══════════════════════════════════════════════════════════════════════
# TELEMETRIA DE LIQUIDEZ ESTRUTURAL — SOMENTE OBSERVABILIDADE
# Não altera avaliar_vortex_decision_layer_v2(), Entry, SL, TP, RR,
# sweep selector, scheduler, Telegram ou resolução de resultado.
# ═══════════════════════════════════════════════════════════════════════
# Universo completo do mapa estrutural. M15 continua visível para execução,
# POIs, obstáculos e alvos locais, mas NÃO pode autorizar sozinho o setup.
KAIROS_STRUCTURAL_LIQUIDITY_TFS = ('MN', 'W1', 'D1', 'H4', 'H1', 'M15')
# Liquidez que pode originar a tese principal do Paper V2.2.
# Prioridade institucional: W1/D1/H4; H1 apenas como estrutura intermediária.
KAIROS_PRIMARY_SETUP_LIQUIDITY_TFS = ('W1', 'D1', 'H4', 'H1')
KAIROS_PRIMARY_LIQUIDITY_PRIORITY = {'W1': 4, 'D1': 3, 'H4': 2, 'H1': 1}
KAIROS_STRUCTURAL_SWING_SIZE = 50


def _kairos_periodo_completo(candle, tf, now_ts):
    """True apenas quando o candle do período já fechou no timestamp auditado."""
    if not candle or candle.get('t') is None:
        return False
    dur = 7 * 86400000 if tf == 'W1' else 86400000 if tf == 'D1' else None
    return bool(dur and candle['t'] + dur <= now_ts)


def _kairos_previous_period_refs(candles_por_tf, now_ts):
    """PDH/PDL/PWH/PWL vêm somente de períodos COMPLETOS anteriores.
    Open atual e close anterior são contexto, nunca liquidity pool.
    """
    out = {
        'PDH': None, 'PDL': None, 'PWH': None, 'PWL': None,
        'daily_open': None, 'previous_daily_close': None,
        'weekly_open': None, 'previous_weekly_close': None,
    }
    for tf, high_key, low_key, open_key, close_key in (
        ('D1', 'PDH', 'PDL', 'daily_open', 'previous_daily_close'),
        ('W1', 'PWH', 'PWL', 'weekly_open', 'previous_weekly_close'),
    ):
        cs = sorted([c for c in (candles_por_tf.get(tf) or []) if c.get('t') is not None and c['t'] <= now_ts], key=lambda x: x['t'])
        if not cs:
            continue
        completos = [c for c in cs if _kairos_periodo_completo(c, tf, now_ts)]
        if completos:
            prev = completos[-1]
            out[high_key] = {'level': prev['h'], 'period_open_ts': prev['t'], 'confirmed_ts': prev['t'] + (7 * 86400000 if tf == 'W1' else 86400000)}
            out[low_key] = {'level': prev['l'], 'period_open_ts': prev['t'], 'confirmed_ts': prev['t'] + (7 * 86400000 if tf == 'W1' else 86400000)}
            out[close_key] = {'level': prev['c'], 'period_open_ts': prev['t'], 'confirmed_ts': prev['t'] + (7 * 86400000 if tf == 'W1' else 86400000), 'role': 'CONTEXT'}
        # candle vigente: último candle cujo open já aconteceu e cujo período ainda não terminou.
        current = next((c for c in reversed(cs) if not _kairos_periodo_completo(c, tf, now_ts)), None)
        if current:
            out[open_key] = {'level': current['o'], 'period_open_ts': current['t'], 'role': 'CONTEXT'}
        elif cs:
            # em fronteira exata, o candle mais novo pode já estar completo; open continua apenas contexto.
            out[open_key] = {'level': cs[-1]['o'], 'period_open_ts': cs[-1]['t'], 'role': 'CONTEXT'}
    return out


def _kairos_lux50_structural_levels(candles, tf, now_ts):
    """Reusa _extrair_swings_lux_algo(swing_size=50) e expõe a confirmação
    causal do próprio algoritmo (origin_idx + swing_size). Não redefine swing.
    """
    cs = [c for c in candles if c.get('t') is not None and c['t'] <= now_ts]
    if len(cs) < KAIROS_STRUCTURAL_SWING_SIZE + 5:
        return []
    swings = _extrair_swings_lux_algo(cs, swing_size=KAIROS_STRUCTURAL_SWING_SIZE)
    by_ts = {c['t']: i for i, c in enumerate(cs)}
    out = []
    for sw in swings:
        oi = by_ts.get(sw.get('t'))
        if oi is None:
            continue
        ci = oi + KAIROS_STRUCTURAL_SWING_SIZE
        if ci >= len(cs):
            continue
        confirm_ts = cs[ci]['t']
        if confirm_ts > now_ts:
            continue
        level = sw.get('valor')
        typ = 'SWING_HIGH' if sw.get('tipo') == 'high' else 'SWING_LOW'
        state = 'ACTIVE'
        captured_ts = None
        for c in cs[ci + 1:]:
            if typ == 'SWING_HIGH' and c['h'] > level:
                state, captured_ts = 'CAPTURED', c['t']; break
            if typ == 'SWING_LOW' and c['l'] < level:
                state, captured_ts = 'CAPTURED', c['t']; break
        out.append({
            'tf': tf, 'type': typ, 'level': level,
            'origin_ts': sw.get('t'), 'confirmed_ts': confirm_ts,
            'state': state, 'captured_ts': captured_ts,
        })
    return out[-40:]


def _kairos_structural_poi_overlaps(levels, candles_por_tf, now_ts):
    """Interseção geométrica entre níveis estruturais e FVG/IFVG/OB existentes."""
    hits = []
    for poi_tf in KAIROS_STRUCTURAL_LIQUIDITY_TFS:
        cs = [c for c in (candles_por_tf.get(poi_tf) or []) if c.get('t') is not None and c['t'] <= now_ts]
        if not cs:
            continue
        zones = _kairos_fvg_states(cs)
        obs = _kairos_order_blocks_map(cs)
        pois = []
        for z in zones:
            if z.get('state') in ('ATIVA', 'TOCADA', 'PARCIAL', 'IFVG'):
                pois.append({'poi_tf': poi_tf, 'poi_type': z.get('tipo'), 'bottom': z.get('bottom'), 'top': z.get('top'), 'state': z.get('state'), 'created_ts': z.get('flip_ts') or z.get('created_ts')})
        for ob in obs:
            if ob.get('state') == 'ATIVA':
                pois.append({'poi_tf': poi_tf, 'poi_type': ob.get('tipo'), 'bottom': ob.get('bottom'), 'top': ob.get('top'), 'state': ob.get('state'), 'created_ts': ob.get('break_ts') or ob.get('t')})
        for liq in levels:
            lv = liq.get('level')
            if lv is None:
                continue
            for poi in pois:
                if poi['bottom'] is None or poi['top'] is None:
                    continue
                if poi['bottom'] <= lv <= poi['top']:
                    hits.append({
                        'liquidity_tf': liq.get('tf'), 'liquidity_type': liq.get('type'),
                        'liquidity_level': lv, **poi,
                    })
    return hits[-80:]



def _kairos_structural_registry(candles_por_tf, now_ts):
    """Registro operacional causal de TODA liquidez estrutural relevante.

    Principal: Lux50 MN/W1/D1/H4/H1/M15 + PDH/PDL/PWH/PWL.
    Adicional: EQH/EQL confirmados nos mesmos TFs quando o mapa causal dispõe deles.
    FVG/IFVG/OB continuam POIs; não são convertidos em liquidez por conveniência.
    """
    levels=[]
    for tf in KAIROS_STRUCTURAL_LIQUIDITY_TFS:
        cs=[c for c in (candles_por_tf.get(tf) or []) if c.get('t') is not None and c['t'] <= now_ts]
        levels.extend(_kairos_lux50_structural_levels(cs, tf, now_ts))
        # EQH/EQL são classe própria de liquidez. Mantemos o detector causal existente.
        if cs:
            data=_kairos_liquidity_map_tf(cs, tf)
            for eq in data.get('equal_liquidity', []):
                level=eq.get('nivel'); cts=eq.get('confirm_ts')
                if level is None or cts is None or cts > now_ts:
                    continue
                levels.append({
                    'tf':tf,'type':eq.get('tipo'),'level':level,
                    'origin_ts':eq.get('origin_ts'),'confirmed_ts':cts,
                    'state':'CAPTURED' if eq.get('state')=='SWEPT' else 'ACTIVE',
                    'captured_ts':eq.get('swept_ts'),'touches':eq.get('toques'),
                })
    refs=_kairos_previous_period_refs(candles_por_tf, now_ts)
    for key,tf in (('PDH','D1'),('PDL','D1'),('PWH','W1'),('PWL','W1')):
        rec=refs.get(key)
        if not rec:
            continue
        typ=key; level=rec['level']; confirmed_ts=rec['confirmed_ts']
        m15=[c for c in (candles_por_tf.get('M15') or []) if c.get('t') is not None and c['t'] <= now_ts]
        is_high=typ in ('PDH','PWH')
        captured=None
        for c in m15:
            if c['t'] <= confirmed_ts:
                continue
            if (is_high and c['h'] > level) or ((not is_high) and c['l'] < level):
                captured=c['t']; break
        levels.append({'tf':tf,'type':typ,'level':level,'origin_ts':rec['period_open_ts'],
                       'confirmed_ts':confirmed_ts,'state':'CAPTURED' if captured else 'ACTIVE',
                       'captured_ts':captured})
    out=[]; seen=set()
    for x in levels:
        if x.get('level') is None: continue
        k=(x.get('tf'),x.get('type'),round(float(x.get('level')),10),x.get('origin_ts'))
        if k in seen: continue
        seen.add(k); out.append(x)
    return out


def _kairos_select_structural_first_capture_sweep(candles_por_tf, now_ts):
    """Primeira captura estrutural NEUTRA.

    A liquidez localiza o evento; NÃO escolhe LONG/SHORT. A primeira captura
    fica congelada e nunca é substituída por uma captura posterior mais bonita.
    Reclaim/rejeição e aceitação/continuação são classificados depois no M15.
    """
    levels=_kairos_structural_registry(candles_por_tf, now_ts)
    # M15 permanece no registry/auditoria, mas não pode ser a liquidez
    # primária que autoriza um setup. MN fica como mapa macro/contexto.
    setup_levels=[x for x in levels if x.get('tf') in KAIROS_PRIMARY_SETUP_LIQUIDITY_TFS]
    m15=[c for c in (candles_por_tf.get('M15') or []) if c.get('t') is not None and c['t'] <= now_ts]
    if len(m15) < 3:
        return None, {'levels':levels,'candidates':[]}
    candidates=[]
    max_age={'MN':45*86400000,'W1':14*86400000,'D1':5*86400000,'H4':48*3600000,'H1':18*3600000,'M15':5*3600000}
    for liq in setup_levels:
        level=liq.get('level'); confirm_ts=liq.get('confirmed_ts')
        if level is None or confirm_ts is None or confirm_ts > now_ts:
            continue
        is_high=liq.get('type') in ('SWING_HIGH','PDH','PWH','PMH','EQH')
        first_idx=None
        for i,c in enumerate(m15):
            if c['t'] <= confirm_ts: continue
            if (is_high and c['h'] > level) or ((not is_high) and c['l'] < level):
                first_idx=i; break
        if first_idx is None: continue
        c=m15[first_idx]
        nxt=m15[first_idx+1] if first_idx+1 < len(m15) else None
        # Estado pós-captura: rejeição/reclaim OU aceitação além do nível.
        if is_high:
            reclaimed=(c['c'] < level) and (nxt is not None and nxt['c'] < level)
            accepted=(c['c'] > level) and (nxt is not None and nxt['c'] > level)
        else:
            reclaimed=(c['c'] > level) and (nxt is not None and nxt['c'] > level)
            accepted=(c['c'] < level) and (nxt is not None and nxt['c'] < level)
        state='REJECTION_RECLAIM' if reclaimed else ('ACCEPTANCE_CONTINUATION' if accepted else 'UNRESOLVED_REACTION')
        rec={'liquidity_tf':liq['tf'],'liquidity_type':liq['type'],'nivel':level,
             'liquidity_origin_ts':liq.get('origin_ts'),'liquidity_confirm_ts':confirm_ts,
             'capture_tf':'M15','first_capture_ts':c['t'],'first_capture_idx':first_idx,
             'extremo':c['h'] if is_high else c['l'],'liquidity_side':'HIGH' if is_high else 'LOW',
             'post_capture_state':state,'first_capture_reclaimed':bool(reclaimed),
             'first_capture_accepted':bool(accepted),'sweep_ts':c['t'],'tf':liq['tf'],
             'confirm_ts':nxt['t'] if nxt is not None and state!='UNRESOLVED_REACTION' else None}
        age=now_ts-c['t']; rec['age_ms']=age
        rec['status']='VALID_FIRST_CAPTURE_NEUTRAL' if state!='UNRESOLVED_REACTION' else 'AWAITING_REACTION'
        candidates.append(rec)
    valid=[x for x in candidates if x.get('status')=='VALID_FIRST_CAPTURE_NEUTRAL' and 0 <= x['age_ms'] <= max_age.get(x['liquidity_tf'],5*3600000)]
    if not valid:
        return None, {'levels':levels,'candidates':candidates}
    # Hierarquia primeiro, recência apenas dentro da mesma classe estrutural.
    # Assim um evento local mais novo nunca atropela W1/D1/H4.
    valid.sort(key=lambda x:(KAIROS_PRIMARY_LIQUIDITY_PRIORITY.get(x['liquidity_tf'],0),x['sweep_ts']), reverse=True)
    return valid[0], {'levels':levels,'setup_levels':setup_levels,'candidates':candidates}


def _kairos_direction_after_first_capture(candles, capture, swing_size=5):
    """Deriva direção da REAÇÃO + intenção + MSS/CHoCH, nunca do lado da liquidez."""
    if not candles or not capture: return None
    side=capture.get('liquidity_side'); state=capture.get('post_capture_state')
    # Quatro caminhos causais AMD/PO3 permitidos.
    expected = ('baixa' if side=='HIGH' else 'alta') if state=='REJECTION_RECLAIM' else ('alta' if side=='HIGH' else 'baixa')
    idx=next((i for i,c in enumerate(candles) if c['t']>=capture['sweep_ts']),None)
    if idx is None: return None
    sub=candles[max(0,idx-swing_size-2):]
    events=compute_lux_internal_structure(sub,swing_size=swing_size)
    for e in events:
        if e.get('t',0) <= capture['sweep_ts'] or e.get('direcao') != expected or e.get('tipo') not in ('CHoCH','BOS'):
            continue
        full_idx=next((j for j,c in enumerate(candles) if c['t']==e['t']),None)
        if full_idx is None: continue
        causal=candles[:full_idx+1]
        z=_kairos_momentum_z(causal)
        atrs=compute_atr(causal,14); atr=next((v for v in reversed(atrs) if v is not None),None)
        bc=candles[full_idx]; body=abs(bc['c']-bc['o'])
        signed_ok=(z is not None and ((expected=='alta' and z>0.5) or (expected=='baixa' and z<-0.5)))
        body_ok=bool(atr and body>=0.5*atr and ((expected=='alta' and bc['c']>bc['o']) or (expected=='baixa' and bc['c']<bc['o'])))
        if signed_ok or body_ok:
            return {'direction':'LONG' if expected=='alta' else 'SHORT','direcao':expected,
                    'mode':'REVERSAL' if state=='REJECTION_RECLAIM' else 'CONTINUATION',
                    'structure':{**e,'full_idx':full_idx},'momentum_z':z}
    return None

def _kairos_m5_refine_zone(m5_candles, m15_zone, structure_ts, direction):
    """Refina uma zona M15 com FVG/IFVG/OB M5 causal. Nunca cria setup sozinho."""
    if not m5_candles or not m15_zone:
        return None
    zones=[]
    for z in _kairos_fvg_states(m5_candles):
        eff=z.get('flip_ts') or z.get('created_ts') or 0
        if eff < structure_ts or z.get('direcao') != direction:
            continue
        if z.get('state') not in ('ATIVA','TOCADA','PARCIAL','IFVG'):
            continue
        if z.get('top') is None or z.get('bottom') is None:
            continue
        if z['top'] < m15_zone['bottom'] or z['bottom'] > m15_zone['top']:
            continue
        q=dict(z); q['source_tf']='M5'; zones.append(q)
    if zones:
        return max(zones,key=lambda z:z.get('flip_ts') or z.get('created_ts') or 0)
    # OB M5 só pode refinar se existir a partir de uma quebra estrutural M5 pós-M15-structure.
    events=compute_lux_internal_structure(m5_candles,swing_size=5)
    wanted=direction
    ev=next((e for e in reversed(events) if e.get('t',0)>=structure_ts and e.get('direcao')==wanted and e.get('tipo') in ('CHoCH','BOS')),None)
    if ev:
        idx=next((i for i,c in enumerate(m5_candles) if c['t']==ev['t']),None)
        ob=_kairos_ob_from_break(m5_candles,idx,wanted) if idx is not None else None
        if ob and not (ob['top'] < m15_zone['bottom'] or ob['bottom'] > m15_zone['top']):
            ob=dict(ob); ob['source_tf']='M5'; return ob
    return None


def _kairos_structural_targets(candles_por_tf, now_ts, entry, direction, limit=12):
    """Próxima liquidez estrutural ATIVA do lado do trade, sem score."""
    levels=_kairos_structural_registry(candles_por_tf, now_ts)
    out=[]
    for x in levels:
        if x.get('state')!='ACTIVE':
            continue
        lv=x.get('level')
        if lv is None: continue
        if direction=='LONG' and lv<=entry: continue
        if direction=='SHORT' and lv>=entry: continue
        q=dict(x); q['nivel']=lv; q['tipo']=x.get('type'); q['dist']=abs(lv-entry); q['classe']='LIQUIDEZ_ESTRUTURAL'
        # M15 pode ser alvo/local obstacle, mas TP2 estrutural deve apontar
        # primeiro para H1/H4/D1/W1 quando houver liquidez ativa nessa direção.
        q['primary_target']=x.get('tf') in KAIROS_PRIMARY_SETUP_LIQUIDITY_TFS
        out.append(q)
    primary=[x for x in out if x.get('primary_target')]
    selected=primary if primary else out
    selected.sort(key=lambda x:(x['dist'],-KAIROS_TF_PESO.get(x.get('tf'),1)))
    return selected[:limit]

def _kairos_build_structural_liquidity_telemetry(candles_por_tf, now_ts, signal_result=None):
    """Snapshot paralelo/auditável. Não participa da autorização do trade."""
    structural = []
    for tf in KAIROS_STRUCTURAL_LIQUIDITY_TFS:
        structural.extend(_kairos_lux50_structural_levels(candles_por_tf.get(tf) or [], tf, now_ts))

    refs = _kairos_previous_period_refs(candles_por_tf, now_ts)
    for key, tf, typ in (
        ('PDH', 'D1', 'PDH'), ('PDL', 'D1', 'PDL'),
        ('PWH', 'W1', 'PWH'), ('PWL', 'W1', 'PWL'),
    ):
        rec = refs.get(key)
        if rec:
            structural.append({'tf': tf, 'type': typ, 'level': rec['level'], 'origin_ts': rec['period_open_ts'], 'confirmed_ts': rec['confirmed_ts'], 'state': 'ACTIVE', 'captured_ts': None})

    # EQH/EQL/pools 20/20 atuais ficam como camada COMPLEMENTAR, somente HTF/M15.
    equal_liquidity = []
    liquidity_pools = []
    for tf in KAIROS_STRUCTURAL_LIQUIDITY_TFS:
        cs = [c for c in (candles_por_tf.get(tf) or []) if c.get('t') is not None and c['t'] <= now_ts]
        if not cs:
            continue
        data = _kairos_liquidity_map_tf(cs, tf)
        for e in data.get('equal_liquidity', []):
            equal_liquidity.append({'tf': tf, **e})
        for pool in data.get('liquidity_pools', []):
            liquidity_pools.append({'tf': tf, **pool})

    overlaps = _kairos_structural_poi_overlaps(structural, candles_por_tf, now_ts)
    result = signal_result or {}
    entry = result.get('entry')
    direction = result.get('direction')
    nearest = None
    if entry is not None and structural:
        wanted = [x for x in structural if (direction == 'LONG' and x['level'] > entry) or (direction == 'SHORT' and x['level'] < entry)]
        if wanted:
            nearest = min(wanted, key=lambda x: abs(x['level'] - entry)).copy()
            nearest['distance_abs'] = abs(nearest['level'] - entry)

    attacked = None
    sweep_level = result.get('sweep_level')
    if sweep_level is not None and structural:
        attacked = min(structural, key=lambda x: abs(x['level'] - sweep_level)).copy()
        attacked['distance_to_existing_sweep'] = abs(attacked['level'] - sweep_level)
        attacked['match_basis'] = 'NEAREST_TO_EXISTING_PAPER_SWEEP'

    return {
        'telemetry_version': 'STRUCTURAL_LIQUIDITY_V2_ACTIVE',
        'asof_ts': now_ts,
        'structural_tfs': list(KAIROS_STRUCTURAL_LIQUIDITY_TFS),
        'structural_liquidity': structural[-160:],
        'previous_periods': {k: refs.get(k) for k in ('PDH', 'PDL', 'PWH', 'PWL')},
        'context_refs': {
            'daily_open': refs.get('daily_open'),
            'previous_daily_close': refs.get('previous_daily_close'),
            'weekly_open': refs.get('weekly_open'),
            'previous_weekly_close': refs.get('previous_weekly_close'),
        },
        'equal_liquidity': equal_liquidity[-80:],
        'liquidity_pools': liquidity_pools[-80:],
        'poi_overlaps': overlaps,
        'nearest_structural_liquidity': nearest,
        'attacked_liquidity': attacked,
        'existing_paper_sweep': {
            'tf': result.get('sweep_tf'), 'level': result.get('sweep_level'),
            'extreme': result.get('sweep_extreme'),
        } if result.get('sweep_tf') else None,
        'structure_event': {
            'choch_timestamp': result.get('choch_timestamp'),
            'choch_level': result.get('choch_level'),
            'execution_tf': result.get('execution_tf'),
        },
        'entry_zone': {
            'type': result.get('zone_type'), 'source': result.get('zone_source'),
            'bottom': result.get('zone_bottom'), 'top': result.get('zone_top'),
            'liquidity_inside_zone': result.get('liquidity_inside_zone') or [],
        },
        'non_gating': False,
    }

def _kairos_opposing_zone_obstacles(mapa, entry, direction, target_level=None, limit=8, allowed_tfs=None):
    """FVG/IFVG/OB contrário no caminho até o pool-alvo."""
    obs=[]
    allowed_tfs=set(allowed_tfs) if allowed_tfs else None
    for tf,data in mapa.items():
        if allowed_tfs is not None and tf not in allowed_tfs:
            continue
        candidates=[]
        for z in data.get('zones',[]):
            if z.get('state') in ('ATIVA','TOCADA','PARCIAL','IFVG'):
                candidates.append(z)
        for ob in data.get('order_blocks',[]):
            if ob.get('state')=='ATIVA': candidates.append(ob)
        for z in candidates:
            zd=z.get('direcao')
            if direction=='LONG' and zd!='baixa': continue
            if direction=='SHORT' and zd!='alta': continue
            bottom=z.get('bottom'); top=z.get('top')
            if bottom is None or top is None: continue
            level=bottom if direction=='LONG' else top
            if direction=='LONG':
                if level<=entry or (target_level is not None and level>=target_level): continue
            else:
                if level>=entry or (target_level is not None and level<=target_level): continue
            obs.append({'tf':tf,'tipo':z.get('tipo','POI_OPPOSTA'),'nivel':level,'top':top,'bottom':bottom,
                        'peso':KAIROS_TF_PESO.get(tf,1),'dist':abs(level-entry),'classe':'OBSTACULO_POI'})
    obs.sort(key=lambda x:(x['dist'],-x['peso']))
    dedup=[]
    for o in obs:
        tol=max(abs(entry)*1e-7,1e-9)
        if any(abs(d['nivel']-o['nivel'])<=tol and d['tipo']==o['tipo'] for d in dedup): continue
        dedup.append(o)
    return dedup[:limit]


def _kairos_zone_contains_liquidity(zone, mapa, max_items=10):
    """Somente pools 2+ toques contam como liquidez dentro da POI."""
    if not zone: return []
    hits=[]
    for tf,data in mapa.items():
        for p in data.get('liquidity_pools',[]):
            if p.get('state')!='ATIVA': continue
            # interseção de zonas, não apenas centro dentro
            if zone['top'] >= p['bottom'] and zone['bottom'] <= p['top']:
                hits.append({'tf':tf,'tipo':p['tipo'],'side':p['side'],'nivel':p['nivel'],
                             'bottom':p['bottom'],'top':p['top'],'toques':p['toques'],
                             'poi_overlaps':p.get('poi_overlaps',[])})
    hits.sort(key=lambda x:(-KAIROS_TF_PESO.get(x['tf'],1),-x.get('toques',0)))
    return hits[:max_items]

def _kairos_select_structural_sl(mapa, exec_tf, exec_candles, context_sweep, structure, retest, direction):
    """Seleciona uma âncora de SL causal e ainda válida no momento da entrada.

    Regras:
    - prioriza sweep do TF de execução, na mesma direção da tese;
    - o sweep local tem de acontecer depois do sweep narrativo e antes/até à quebra estrutural;
    - uma âncora cujo SL buffered já tenha sido negociado antes da entrada é descartada;
    - se nenhum sweep local servir, tenta o sweep narrativo HTF;
    - nunca força stop do lado errado só para fabricar RR.
    """
    if not exec_candles or not context_sweep or not structure or not retest:
        return None, {'motivo': 'DADOS_SL_INSUFICIENTES', 'candidatos': []}

    entry = retest['c']
    thesis_dir = context_sweep['direcao']
    struct_ts = structure['t']
    retest_ts = retest['t']
    context_ts = context_sweep['sweep_ts']

    local_sweeps = (mapa.get(exec_tf) or {}).get('sweeps', [])
    locais = [
        x for x in local_sweeps
        if x.get('direcao') == thesis_dir
        and context_ts <= x.get('sweep_ts', -1) <= struct_ts
    ]
    locais.sort(key=lambda x: x.get('sweep_ts', 0), reverse=True)

    candidatos = [(exec_tf, x, 'EXECUCAO') for x in locais]
    # Sweep narrativo sempre fica como fallback estrutural, sem duplicar o mesmo evento.
    if not any(x.get('sweep_ts') == context_sweep.get('sweep_ts') and tf == context_sweep.get('tf')
               for tf, x, _ in candidatos):
        candidatos.append((context_sweep.get('tf'), context_sweep, 'NARRATIVA'))

    audit=[]
    for tf_anchor, sw, classe in candidatos:
        base = sw.get('extremo')
        candles_ate_entry = [c for c in exec_candles if c['t'] <= retest_ts]
        sl = aplicar_buffer_stop_atr(base, thesis_dir, candles_ate_entry)
        rec = {
            'tf': tf_anchor, 'classe': classe, 'sweep_ts': sw.get('sweep_ts'),
            'sweep_level': sw.get('nivel'), 'sweep_extreme': base, 'sl_buffered': sl,
        }
        if sl is None:
            rec['status']='SEM_SL'; audit.append(rec); continue

        right = (sl < entry) if direction == 'LONG' else (sl > entry)
        if not right:
            rec['status']='LADO_ERRADO'; audit.append(rec); continue

        # A âncora não pode já ter sido violada ANTES da decisão de entrada.
        posteriores = [c for c in exec_candles if sw.get('sweep_ts', 0) < c['t'] < retest_ts]
        if direction == 'LONG':
            violacao = next((c for c in posteriores if c['l'] <= sl), None)
        else:
            violacao = next((c for c in posteriores if c['h'] >= sl), None)
        if violacao:
            rec['status']='INVALIDADA_ANTES_ENTRY'; rec['invalidated_ts']=violacao['t']; audit.append(rec); continue

        rec['status']='VALIDA'; audit.append(rec)
        return {
            'sl': sl, 'sl_base': base, 'sl_tf': tf_anchor, 'sl_classe': classe,
            'sl_sweep_ts': sw.get('sweep_ts'), 'sl_sweep_level': sw.get('nivel'),
            'sl_sweep_extreme': base,
        }, {'motivo': 'OK', 'candidatos': audit}

    return None, {'motivo': 'SEM_ANCORA_SL_CAUSAL_VALIDA', 'candidatos': audit}


def _kairos_select_entry_zone(exec_candles, sweep, structure, mapa):
    """Zona causal sem score: IFVG/FVG/OB ligados ao evento, por ordem temporal."""
    if not structure:
        return None
    direction=sweep['direcao']
    st=structure['t']
    zones=[]
    for z in _kairos_fvg_states(exec_candles):
        effective_ts=z.get('flip_ts') or z.get('created_ts') or 0
        if z.get('direcao') != direction:
            continue
        if z.get('state') not in ('ATIVA','TOCADA','PARCIAL','IFVG'):
            continue
        if not (sweep['sweep_ts'] <= effective_ts <= st):
            continue
        z2=dict(z); z2['liquidity_inside']=_kairos_zone_contains_liquidity(z2,mapa)
        zones.append(z2)

    # Prioridade operacional: zona causal que contém/intersecta pool de liquidez,
    # depois IFVG e FVG causais. Não é score; é relação geométrica POI<->liquidez.
    with_pool=[z for z in zones if z.get('liquidity_inside')]
    if with_pool:
        ifvg_pool=[z for z in with_pool if z['tipo'].startswith('IFVG')]
        if ifvg_pool:
            return max(ifvg_pool,key=lambda z:(z.get('flip_ts') or z.get('created_ts') or 0))
        return max(with_pool,key=lambda z:(z.get('created_ts') or 0))
    ifvg=[z for z in zones if z['tipo'].startswith('IFVG')]
    fvg=[z for z in zones if z['tipo'].startswith('FVG')]
    if ifvg:
        return max(ifvg,key=lambda z:(z.get('flip_ts') or z.get('created_ts') or 0))
    if fvg:
        return max(fvg,key=lambda z:(z.get('created_ts') or 0))

    # OB é derivado diretamente do break e só entra se não houver imbalance causal.
    ob=_kairos_ob_from_break(exec_candles, structure.get('full_idx'), direction)
    if ob:
        ob['liquidity_inside']=_kairos_zone_contains_liquidity(ob,mapa)
        return ob
    return None

def _kairos_shadow_validate_poi(zone, candles, sweep=None, structure=None, tf='M15'):
    """Auditoria paralela FVG/IFVG/OB. NÃO altera autorização do trade.

    Recalcula a origem geométrica/casual do POI selecionado e devolve PASS/FAIL
    com evidência suficiente para reproduzir no gráfico. FVG/IFVG usa a mesma
    regra 3-candles em qualquer TF; IFVG exige FVG-mãe + flip posterior. OB
    exige candle oposto anterior ao break e break posterior ao sweep.
    """
    out={'shadow_only':True,'tf':tf,'pass':False,'reason':None,'tipo':zone.get('tipo') if zone else None}
    if not zone:
        out['reason']='SEM_ZONA'; return out
    typ=str(zone.get('tipo') or '')
    if typ.startswith(('FVG_','IFVG_')):
        a=zone.get('source_a'); b=zone.get('source_mid'); c=zone.get('source_c')
        if not all(isinstance(x,dict) for x in (a,b,c)):
            out['reason']='SEM_CANDLES_ORIGEM_ABC'; return out
        ordered=(a.get('t') is not None and b.get('t') is not None and c.get('t') is not None and a['t'] < b['t'] < c['t'])
        bull_geom=(c.get('l') is not None and a.get('h') is not None and c['l'] > a['h'])
        bear_geom=(c.get('h') is not None and a.get('l') is not None and c['h'] < a['l'])
        mother='FVG_bullish' if bull_geom else ('FVG_bearish' if bear_geom else None)
        bounds_ok=False
        if mother=='FVG_bullish': bounds_ok=abs(float(zone['bottom'])-float(a['h']))<1e-9 and abs(float(zone['top'])-float(c['l']))<1e-9
        elif mother=='FVG_bearish': bounds_ok=abs(float(zone['top'])-float(a['l']))<1e-9 and abs(float(zone['bottom'])-float(c['h']))<1e-9
        created_ok=zone.get('created_ts')==c.get('t')
        causal_ok=True
        if sweep: causal_ok=causal_ok and zone.get('created_ts',0) >= sweep.get('sweep_ts',0)
        if structure: causal_ok=causal_ok and (zone.get('flip_ts') or zone.get('created_ts') or 0) <= structure.get('t',0)
        out.update({'mother_type':mother,'ordered_abc':ordered,'geometry_ok':bool(mother),'bounds_ok':bounds_ok,
                    'created_ts_ok':created_ok,'causal_window_ok':causal_ok,'source_a':a,'source_mid':b,'source_c':c,
                    'top':zone.get('top'),'bottom':zone.get('bottom'),'created_ts':zone.get('created_ts'),'flip_ts':zone.get('flip_ts')})
        if typ.startswith('IFVG_'):
            flip=zone.get('flip_candle'); fts=zone.get('flip_ts')
            flip_after=bool(isinstance(flip,dict) and fts==flip.get('t') and fts and fts>zone.get('created_ts',0))
            if mother=='FVG_bearish': flip_geom=bool(flip and flip.get('c') is not None and flip['c'] > zone['top'] and typ=='IFVG_bullish')
            elif mother=='FVG_bullish': flip_geom=bool(flip and flip.get('c') is not None and flip['c'] < zone['bottom'] and typ=='IFVG_bearish')
            else: flip_geom=False
            out.update({'flip_after_creation':flip_after,'flip_geometry_ok':flip_geom,'flip_candle':flip})
            ok=ordered and bool(mother) and bounds_ok and created_ok and causal_ok and flip_after and flip_geom
            out['pass']=bool(ok); out['reason']='OK' if ok else 'IFVG_SEM_CADEIA_MAE_FLIP_CAUSAL_VALIDA'
            return out
        expected='FVG_bullish' if mother=='FVG_bullish' else ('FVG_bearish' if mother=='FVG_bearish' else None)
        ok=ordered and bool(mother) and typ==expected and bounds_ok and created_ok and causal_ok
        out['pass']=bool(ok); out['reason']='OK' if ok else 'FVG_GEOMETRIA_OU_CAUSALIDADE_INVALIDA'
        return out
    if typ.startswith('OB_'):
        t=zone.get('t'); idx=zone.get('idx'); break_idx=zone.get('break_idx')
        idx_ok=isinstance(idx,int) and 0<=idx<len(candles) and candles[idx].get('t')==t
        break_ok=isinstance(break_idx,int) and 0<=break_idx<len(candles) and idx_ok and idx<break_idx
        direction='alta' if typ=='OB_bullish' else 'baixa'
        candle=candles[idx] if idx_ok else None
        opposite=bool(candle and ((direction=='alta' and candle['c']<candle['o']) or (direction=='baixa' and candle['c']>candle['o'])))
        bounds_ok=bool(candle and abs(float(zone['top'])-float(candle['h']))<1e-9 and abs(float(zone['bottom'])-float(candle['l']))<1e-9)
        causal_ok=True
        if sweep and break_ok: causal_ok=candles[break_idx]['t']>sweep.get('sweep_ts',0)
        if structure and break_ok: causal_ok=causal_ok and candles[break_idx]['t']==structure.get('t')
        ok=idx_ok and break_ok and opposite and bounds_ok and causal_ok
        break_candle=candles[break_idx] if break_ok else None
        out.update({'pass':bool(ok),'reason':'OK' if ok else 'OB_ORIGEM_OU_BREAK_CAUSAL_INVALIDO','origin_candle':candle,
                    'break_candle':break_candle,'idx_ok':idx_ok,'break_ok':break_ok,'opposite_candle_ok':opposite,
                    'bounds_ok':bounds_ok,'causal_ok':causal_ok})
        return out
    out['reason']='TIPO_POI_DESCONHECIDO'; return out


_KAIROS_POI_SHADOW_SEEN = set()
_KAIROS_POI_SHADOW_SEEN_MAX = 5000

def _kairos_shadow_log_poi(pair, zone, audit):
    """Shadow log deduplicado. Nunca altera a decisão do trade."""
    try:
        pair_label = pair or 'NA'
        tf = audit.get('tf')
        tipo = audit.get('tipo')
        bottom, top = zone.get('bottom'), zone.get('top')
        created = zone.get('created_ts') or zone.get('t')
        flip_ts = zone.get('flip_ts')
        key = (pair_label, tf, tipo, created, flip_ts, bottom, top, audit.get('reason'), bool(audit.get('pass')))
        if key in _KAIROS_POI_SHADOW_SEEN:
            return
        if len(_KAIROS_POI_SHADOW_SEEN) >= _KAIROS_POI_SHADOW_SEEN_MAX:
            _KAIROS_POI_SHADOW_SEEN.clear()
        _KAIROS_POI_SHADOW_SEEN.add(key)

        base = (f"[POI_SHADOW_AUDIT] pair={pair_label} tf={tf} tipo={tipo} "
                f"pass={audit.get('pass')} reason={audit.get('reason')} "
                f"zone=[{bottom},{top}] created={created} flip={flip_ts}")

        if str(tipo or '').startswith(('FVG_', 'IFVG_')):
            print(base
                  + f" mother={audit.get('mother_type')}"
                  + f" geometry_ok={audit.get('geometry_ok')}"
                  + f" bounds_ok={audit.get('bounds_ok')}"
                  + f" created_ok={audit.get('created_ts_ok')}"
                  + f" causal_ok={audit.get('causal_window_ok')}"
                  + f" flip_after={audit.get('flip_after_creation')}"
                  + f" flip_geometry_ok={audit.get('flip_geometry_ok')}"
                  + f" A={audit.get('source_a')}"
                  + f" B={audit.get('source_mid')}"
                  + f" C={audit.get('source_c')}"
                  + f" FLIP={audit.get('flip_candle')}")
        elif str(tipo or '').startswith('OB_'):
            print(base
                  + f" origin={audit.get('origin_candle')}"
                  + f" break={audit.get('break_candle')}"
                  + f" idx_ok={audit.get('idx_ok')}"
                  + f" break_ok={audit.get('break_ok')}"
                  + f" opposite_ok={audit.get('opposite_candle_ok')}"
                  + f" bounds_ok={audit.get('bounds_ok')}"
                  + f" causal_ok={audit.get('causal_ok')}")
        else:
            print(base)
    except Exception as exc:
        print(f"[POI_SHADOW_AUDIT_ERR] {exc}")

def _kairos_retest_zone(candles, zone, after_ts):
    if not zone:
        return None
    for c in candles:
        if c['t'] <= after_ts:
            continue
        if c['h'] >= zone['bottom'] and c['l'] <= zone['top']:
            return c
    return None


def _kairos_context_bias(candles_por_tf):
    """Narrativa HTF hierárquica; contexto, NUNCA trava de direção.

    MN/W1/D1 descrevem o fluxo macro. H4/H1 descrevem a perna interna.
    Não existe votação: cada TF mantém sua própria leitura. `final` é apenas
    a referência operacional mais próxima disponível (D1 -> W1 -> MN -> H4 -> H1).
    A direção do trade continua vindo da reação pós-liquidez + estrutura M15.
    """
    out={}
    for tf,size in (('MN',50),('W1',50),('D1',50),('H4',50),('H1',50)):
        cs=candles_por_tf.get(tf) or []
        out[tf]=compute_lux_structure_bias(cs,swing_size=min(size,max(5,len(cs)//3))) if len(cs)>=12 else 'neutro'
    final='neutro'
    for tf in ('D1','W1','MN','H4','H1'):
        if out.get(tf) in ('alta','baixa'):
            final=out[tf]; break
    out['macro']={'MN':out.get('MN'),'W1':out.get('W1'),'D1':out.get('D1')}
    out['internal']={'H4':out.get('H4'),'H1':out.get('H1')}
    out['final']=final
    return out



def avaliar_vortex_decision_layer_v2(m15_ate_agora, m5_ate_agora, d1_ate_agora=None,
                                      candles_por_tf=None, audit_pair=None):
    """KAIROS Paper V2.1 — liquidez estrutural ativa, M15 executa, M5 refina.

    Cadeia autorizadora:
    HTF/M15 structural liquidity -> neutral FIRST capture on M15 -> rejection/reclaim OR acceptance/continuation -> intention -> M15 MSS/CHoCH/BOS
    -> displacement -> causal M15 FVG/IFVG/OB -> optional M5 refinement -> retest
    -> SL behind causal sweep -> nearest active structural liquidity/obstacle TP.

    W1/D1/H4 podem originar a tese principal; H1 pode atuar como estrutura intermediária relevante.\n    M15/M5 executam, refinam e podem servir como alvo/obstáculo local; não autorizam sozinhos o setup. M1 não participa.
    """
    resultado={
        'signal':False,'direction':None,'bias':None,'zone_type':None,'zone_top':None,'zone_bottom':None,'zone_source':None,
        'choch_confirmed':False,'choch_timestamp':None,'choch_level':None,'entry':None,'sl':None,'sl_regra':None,
        'tp':None,'tp_origem':None,'rr':None,'reason':None,'timestamp':m15_ate_agora[-1]['t'] if m15_ate_agora else None,
        'valid':False,'failure_reason':None,'variante':'KAIROS_V2_2_TP1_TP2_CORRIGIDO',
        'setup_type':None,'context_bias':None,'sweep_tf':None,'sweep_level':None,'sweep_extreme':None,
        'liquidity_tf':None,'liquidity_type':None,'capture_tf':None,'first_capture_ts':None,'sweep_confirm_ts':None,
        'execution_tf':None,'refinement_tf':None,'momentum_z':None,'liquidity_inside_zone':[],
        'next_liquidity_targets':[],'target_obstacles':[],'first_liquidity_target':None,'tp_final_liquidez':None,
        'tp1_obstacle':None,'tp1':None,'tp1_origem':None,'tp1_rr':None,'tp2':None,'tp2_origem':None,'tp2_rr':None,'tp_horizon_tfs':[],'tp_horizon_mode':'STRUCTURAL_INTRADAY',
        'mtf_summary':{},'sl_audit':None,'sl_anchor_tf':None,'sl_anchor_class':None,
        'sl_anchor_sweep_ts':None,'sl_anchor_extreme':None,'structural_sweep_audit':None,
        'prealert_limit':None,'prealert_sl':None,'prealert_tp':None,'prealert_rr':None,
        'prealert_tp_origem':None,'prealert_tp1':None,'prealert_tp1_origem':None,'prealert_tp1_rr':None,'prealert_tp2':None,'prealert_tp2_origem':None,'prealert_tp2_rr':None,'prealert_zone_tf':None,
    }
    if not m15_ate_agora or not m5_ate_agora:
        resultado['failure_reason']='CANDLES_INSUFICIENTES'; return resultado
    if candles_por_tf is None:
        candles_por_tf={'D1':d1_ate_agora or [],'M15':m15_ate_agora,'M5':m5_ate_agora}
    else:
        candles_por_tf=dict(candles_por_tf)
        candles_por_tf.setdefault('D1',d1_ate_agora or [])
        candles_por_tf.setdefault('M15',m15_ate_agora)
        candles_por_tf.setdefault('M5',m5_ate_agora)

    now_ts=max((cs[-1]['t'] for cs in candles_por_tf.values() if cs),default=resultado['timestamp'] or 0)
    mapa=_kairos_build_mtf_map(candles_por_tf)
    contexto=_kairos_context_bias(candles_por_tf)
    resultado['context_bias']=contexto; resultado['bias']=contexto.get('final')
    resultado['mtf_summary']={tf:{
        'pivots':len(d.get('pivots',[])),'eq':len(d.get('equal_liquidity',[])),
        'liq_ativas':sum(1 for x in d.get('liquidity_pools',[]) if x.get('state')=='ATIVA'),
        'liquidity_pools':len(d.get('liquidity_pools',[])),'pools_em_poi':sum(1 for x in d.get('liquidity_pools',[]) if x.get('poi_overlaps')),
        'sweeps':len(d.get('sweeps',[])),'sweeps_confirmados':sum(1 for x in d.get('sweeps',[]) if x.get('confirmado_3b')),
        'zones':len(d.get('zones',[])),'order_blocks':len(d.get('order_blocks',[])),'volume':d.get('volume')
    } for tf,d in mapa.items()}

    sweep,sweep_audit=_kairos_select_structural_first_capture_sweep(candles_por_tf,now_ts)
    resultado['structural_sweep_audit']=sweep_audit
    if not sweep:
        resultado['failure_reason']='SEM_SWEEP_ESTRUTURAL_FIRST_CAPTURE_VALIDO'; return resultado

    resultado['sweep_tf']=sweep['liquidity_tf']; resultado['liquidity_tf']=sweep['liquidity_tf']
    resultado['liquidity_type']=sweep['liquidity_type']; resultado['capture_tf']='M15'
    resultado['first_capture_ts']=sweep['first_capture_ts']; resultado['sweep_confirm_ts']=sweep.get('confirm_ts')
    resultado['sweep_level']=round(sweep['nivel'],6); resultado['sweep_extreme']=round(sweep['extremo'],6)

    # M15 lê intenção/estrutura APÓS a captura neutra e só então escolhe LONG/SHORT.
    exec_tf='M15'; exec_candles=candles_por_tf.get('M15') or []
    intent=_kairos_direction_after_first_capture(exec_candles,sweep,swing_size=5)
    if not intent:
        resultado['failure_reason']='SEM_INTENCAO_CHOCH_MSS_M15_APOS_FIRST_CAPTURE'; return resultado
    direction=intent['direction']; sweep['direcao']=intent['direcao']; structure=intent['structure']
    resultado['direction']=direction; resultado['execution_tf']='M15'; resultado['choch_confirmed']=True
    resultado['choch_timestamp']=structure['t']; resultado['choch_level']=round(structure['nivel'],6)
    z=intent.get('momentum_z'); resultado['momentum_z']=round(z,3) if z is not None else None

    ctx=contexto.get('final'); trade_dir=intent['direcao']
    if ctx==trade_dir: resultado['setup_type']='TREND'
    elif intent['mode']=='CONTINUATION': resultado['setup_type']='INTERNAL_CONTINUATION'
    elif ctx in ('alta','baixa'): resultado['setup_type']='PULLBACK_REVERSAL'
    else: resultado['setup_type']='LOCAL'

    zone=_kairos_select_entry_zone(exec_candles,sweep,structure,mapa)
    if not zone:
        resultado['failure_reason']='SEM_FVG_IFVG_OB_M15_CAUSAL'; return resultado
    # SHADOW ONLY: prova matemática/causal do POI escolhido. Não bloqueia nem altera sinal.
    try:
        poi_shadow=_kairos_shadow_validate_poi(zone, exec_candles, sweep=sweep, structure=structure, tf='M15')
        resultado['poi_shadow_audit']=poi_shadow
        _kairos_shadow_log_poi(audit_pair, zone, poi_shadow)
    except Exception as _poi_shadow_exc:
        resultado['poi_shadow_audit']={'shadow_only':True,'pass':False,'reason':f'AUDIT_EXCEPTION:{_poi_shadow_exc}'}
    resultado['zone_type']=zone['tipo']; resultado['zone_top']=round(zone['top'],6); resultado['zone_bottom']=round(zone['bottom'],6)
    resultado['zone_source']=f"{zone['tipo']}_M15_APOS_SWEEP"; resultado['liquidity_inside_zone']=zone.get('liquidity_inside',[])
    # Auditoria causal da zona: não altera seleção/entrada; apenas expõe os candles exatos.
    resultado['zone_created_ts']=zone.get('created_ts')
    resultado['zone_origin_ts']=zone.get('origin_ts')
    resultado['zone_flip_ts']=zone.get('flip_ts')
    resultado['zone_source_a']=zone.get('source_a')
    resultado['zone_source_mid']=zone.get('source_mid')
    resultado['zone_source_c']=zone.get('source_c')
    resultado['zone_flip_candle']=zone.get('flip_candle')
    zone_ts=zone.get('flip_ts') or zone.get('created_ts') or zone.get('t') or structure['t']
    after_ts=max(structure['t'],zone_ts)

    # M5 refinement é opcional e nunca inventa setup sem a zona M15.
    m5=candles_por_tf.get('M5') or []
    refined=_kairos_m5_refine_zone(m5,zone,structure['t'],sweep['direcao'])
    retest=None; active_zone=zone; entry_tf='M15'
    if refined:
        rz_ts=refined.get('flip_ts') or refined.get('created_ts') or refined.get('t') or structure['t']
        r5=_kairos_retest_zone(m5,refined,max(structure['t'],rz_ts))
        if r5:
            retest=r5; active_zone=refined; entry_tf='M5'; resultado['refinement_tf']='M5'
            resultado['zone_type']=refined.get('tipo',resultado['zone_type'])
            resultado['zone_top']=round(refined['top'],6); resultado['zone_bottom']=round(refined['bottom'],6)
            resultado['zone_source']=f"{refined.get('tipo','POI')}_M5_REFINO_DENTRO_M15"
    if retest is None:
        retest=_kairos_retest_zone(exec_candles,zone,after_ts)
    if not retest:
        # PRE-ALERTA: o setup já está armado, mas o preço ainda NÃO retestou a zona.
        # A referência de LIMIT é o CE (50%) da zona ativa. Isto é apenas para observação/manual demo;
        # NÃO altera a regra oficial abaixo, que continua exigindo reteste e usa o close do reteste.
        pre_limit = (float(active_zone['top']) + float(active_zone['bottom'])) / 2.0
        resultado['prealert_limit'] = round(pre_limit, 6)
        resultado['prealert_zone_tf'] = entry_tf

        # SL de referência estritamente atrás do sweep estrutural narrativo + buffer ATR,
        # sem permitir que o pré-alerta invente uma âncora local diferente.
        try:
            if intent.get('mode')=='CONTINUATION':
                seg=[c for c in exec_candles if sweep['sweep_ts'] <= c['t'] <= structure['t']]
                base=(min(c['l'] for c in seg) if direction=='LONG' else max(c['h'] for c in seg)) if seg else sweep.get('extremo')
                pre_sl=aplicar_buffer_stop_atr(base, sweep['direcao'], exec_candles)
            else:
                pre_sl=aplicar_buffer_stop_atr(sweep.get('extremo'), sweep['direcao'], exec_candles)
        except Exception:
            pre_sl = None
        if pre_sl is not None:
            right_side = (pre_sl < pre_limit) if direction == 'LONG' else (pre_sl > pre_limit)
            if right_side:
                pre_risk = abs(pre_limit - pre_sl)
                resultado['prealert_sl'] = round(pre_sl, 6)
                if pre_risk > 0:
                    pre_targets = _kairos_structural_targets(
                        candles_por_tf, now_ts, pre_limit, direction, limit=12
                    )
                    if pre_targets:
                        pre_target = pre_targets[0]
                        pre_obstacles = _kairos_opposing_zone_obstacles(
                            mapa, pre_limit, direction,
                            target_level=pre_target['nivel'], limit=8,
                            allowed_tfs=('M15','H1','H4','D1','W1')
                        )
                        # TP1 = primeiro obstáculo relevante (gestão/parcial).
                        # TP2/TP final = liquidez estrutural causal. O obstáculo NÃO encurta
                        # automaticamente o alvo final nem mata o setup pelo RR do TP1.
                        if pre_obstacles:
                            pre_o = pre_obstacles[0]
                            pre_tp1 = float(pre_o['nivel'])
                            pre_tp1_rr = abs(pre_tp1 - pre_limit) / pre_risk
                            resultado['prealert_tp1'] = round(pre_tp1, 6)
                            resultado['prealert_tp1_rr'] = round(pre_tp1_rr, 2)
                            resultado['prealert_tp1_origem'] = f"OBSTACULO_{pre_o['tf']}_{pre_o['tipo']}"
                        pre_tp2 = float(pre_target['nivel'])
                        pre_tp2_rr = abs(pre_tp2 - pre_limit) / pre_risk
                        resultado['prealert_tp2'] = round(pre_tp2, 6)
                        resultado['prealert_tp2_rr'] = round(pre_tp2_rr, 2)
                        resultado['prealert_tp2_origem'] = f"LIQUIDEZ_ESTRUTURAL_{pre_target['tf']}_{pre_target['tipo']}"
                        # Compatibilidade com tabela/Telegram antigos: tp_ref passa a ser o TP final estrutural.
                        resultado['prealert_tp'] = resultado['prealert_tp2']
                        resultado['prealert_rr'] = resultado['prealert_tp2_rr']
                        resultado['prealert_tp_origem'] = resultado['prealert_tp2_origem']

        resultado['failure_reason']='AGUARDANDO_RETESTE_ZONA'; return resultado
    entry=retest['c']; resultado['entry']=round(entry,6); resultado['timestamp']=retest['t']

    # SL atrás do sweep estrutural que autorizou a tese; M5 não move o stop para o lado errado.
    if intent.get('mode')=='CONTINUATION':
        seg=[c for c in exec_candles if sweep['sweep_ts'] <= c['t'] <= structure['t']]
        base=(min(c['l'] for c in seg) if direction=='LONG' else max(c['h'] for c in seg)) if seg else None
        slc=aplicar_buffer_stop_atr(base,sweep['direcao'],[c for c in exec_candles if c['t']<=retest['t']]) if base is not None else None
        right=(slc < retest['c']) if direction=='LONG' else (slc > retest['c'])
        sl_info={'sl':slc,'sl_base':base,'sl_tf':'M15','sl_classe':'CONTINUATION_STRUCTURE','sl_sweep_ts':sweep['sweep_ts'],'sl_sweep_level':sweep['nivel'],'sl_sweep_extreme':base} if slc is not None and right else None
        sl_audit={'motivo':'OK_CONTINUATION_STRUCTURE' if sl_info else 'SEM_ANCORA_CONTINUATION_VALIDA','candidatos':[]}
    else:
        sl_info,sl_audit=_kairos_select_structural_sl(mapa,'M15',exec_candles,sweep,structure,retest,direction)
    resultado['sl_audit']=sl_audit
    if not sl_info:
        resultado['failure_reason']='SEM_ANCORA_SL_CAUSAL_VALIDA'; return resultado
    sl=sl_info['sl']; risk=abs(entry-sl)
    if risk<=0:
        resultado['failure_reason']='RISCO_ZERO_SL'; return resultado
    resultado['sl']=round(sl,6); resultado['sl_regra']=(f"continuation_structure_M15_atr" if intent.get('mode')=='CONTINUATION' else f"first_capture_{sweep['liquidity_tf']}_extremo_atr")
    resultado['sl_anchor_tf']=sweep['liquidity_tf']; resultado['sl_anchor_class']='STRUCTURAL_FIRST_CAPTURE'
    resultado['sl_anchor_sweep_ts']=sweep['sweep_ts']; resultado['sl_anchor_extreme']=round(sweep['extremo'],6)

    # TP = primeira liquidez estrutural ATIVA do lado do trade. POI contrário antes dela pode virar TP conservador.
    targets=_kairos_structural_targets(candles_por_tf,now_ts,entry,direction,limit=12)
    for t in targets: t['rr']=round(t['dist']/risk,2) if risk else None
    resultado['next_liquidity_targets']=targets[:8]
    if not targets:
        resultado['failure_reason']='SEM_LIQUIDEZ_ESTRUTURAL_ALVO'; return resultado
    target=targets[0]; resultado['first_liquidity_target']=dict(target); resultado['tp_final_liquidez']=round(target['nivel'],6)

    obstacle_tfs=('M15','H1','H4','D1','W1')
    obstacles=_kairos_opposing_zone_obstacles(mapa,entry,direction,target_level=target['nivel'],limit=8,allowed_tfs=obstacle_tfs)
    for o in obstacles: o['rr']=round(o['dist']/risk,2) if risk else None
    resultado['target_obstacles']=obstacles
    # TP1 é gestão/parcial no primeiro obstáculo; TP2 é o alvo estrutural final.
    # O filtro de RR usa TP2, evitando rejeitar um setup bom só porque existe um
    # FVG/IFVG/OB próximo. Não ignoramos o obstáculo: ele fica explícito como TP1.
    if obstacles:
        tp1=obstacles[0]
        resultado['tp1_obstacle']=dict(tp1)
        resultado['tp1']=round(float(tp1['nivel']),6)
        resultado['tp1_rr']=round(abs(float(tp1['nivel'])-entry)/risk,2)
        resultado['tp1_origem']=f"OBSTACULO_{tp1['tf']}_{tp1['tipo']}"

    tp2=float(target['nivel'])
    rr2=abs(tp2-entry)/risk
    resultado['tp2']=round(tp2,6)
    resultado['tp2_rr']=round(rr2,2)
    resultado['tp2_origem']=f"LIQUIDEZ_ESTRUTURAL_{target['tf']}_{target['tipo']}"

    min_rr=1.0 if resultado['setup_type'] in ('PULLBACK','PULLBACK_REVERSAL','INTERNAL_CONTINUATION') else 2.0
    if rr2 < min_rr:
        resultado['rr']=round(rr2,2); resultado['failure_reason']='ALVO_ESTRUTURAL_RR_INSUFICIENTE'; return resultado
    resultado['tp']=resultado['tp2']; resultado['rr']=resultado['tp2_rr']; resultado['tp_origem']=resultado['tp2_origem']

    resultado['signal']=True; resultado['valid']=True
    resultado['reason']=(
        f"LIQ={sweep['liquidity_tf']}:{sweep['liquidity_type']}@{sweep['nivel']} -> FIRST_CAPTURE_M15@{sweep['sweep_ts']} -> "
        f"{sweep.get('post_capture_state')} -> INTENT={intent['mode']}:{direction} -> {structure['tipo']}_M15 -> DISPLACEMENT(z={resultado['momentum_z']}) -> "
        f"{resultado['zone_source']} -> RETEST_{entry_tf} -> SL_FIRST_CAPTURE -> {resultado['tp_origem']} RR={resultado['rr']} -> "
        f"SETUP={resultado['setup_type']}"
    )
    return resultado


# ═══════════════════════════════════════════════════════════════════════
# REPLAY — avaliar_vortex_decision_layer_v2 — item aprovado do ticket.
# SOMENTE REPLAY/AUDITORIA, sem deploy, sem alterar produção. Roda o
# pipeline experimental candle a candle, causal, sem lookahead, e
# agrega funil completo + distribuição de R:R + exemplos + MFE/MAE
# causal (reaproveitando _medir_mfe_mae_janela/_agregar_mfe_mae já
# testados). NÃO chama process_pair_gates_vortex() nem
# process_pair_4camadas() nem avaliar_vortex_decision_layer() (v1).
# ═══════════════════════════════════════════════════════════════════════

VORTEX_DECISION_LAYER_V2_VERSAO = 'avaliar_vortex_decision_layer_v2 — pipeline BIAS→ZONA→CHoCH_M5→ENTRY→SL→TP→RR, sem alteração desde a Execução 1/2 de 7 dias'


def replay_vortex_decision_layer_v2(pair, dias_historico=7, janelas_mfe_mae=JANELAS_MFE_MAE_PADRAO, fim_ts_ms=None):
    """
    Replay causal completo do pipeline v2 (BIAS→ZONA→CHoCH M5→ENTRY→
    SL→TP→RR). Mesma metodologia já aprovada (fetch único por
    timeframe, truncamento causal m15/d1 pelo mesmo ts_corte do ciclo
    M5). Deduplica sinais válidos por (choch_timestamp, direction,
    zone_type) — o mesmo sinal permanece "válido" em vários ciclos
    consecutivos até ser invalidado; reportamos tanto o total bruto de
    avaliações quanto os sinais únicos.

    fim_ts_ms — PARÂMETRO OPCIONAL, ADITIVO (default None = janela
    rolante a partir de "agora", comportamento idêntico ao das
    execuções anteriores). Quando fornecido, ancora o fim da janela
    histórica nesse timestamp fixo (ms) — a janela [inicio, fim] fica
    reproduzível, não desloca com o tempo real entre execuções. Se não
    for passado explicitamente, esta função fixa "agora" no início da
    chamada (int(time.time()*1000)) e registra esse valor no
    resultado, pra permitir reprodução exata numa chamada futura.
    """
    if fim_ts_ms is None:
        fim_ts_ms = int(time.time() * 1000)

    symbol_map = {
        'BTCUSD': 'BTCUSDT', 'ETHUSD': 'ETHUSDT', 'SOLUSD': 'SOLUSDT', 'XRPUSD': 'XRPUSDT',
        'LINKUSD': 'LINKUSDT', 'ADAUSD': 'ADAUSDT', 'AVAXUSD': 'AVAXUSDT', 'BNBUSD': 'BNBUSDT',
        'AAVEUSD': 'AAVEUSDT', 'NEARUSD': 'NEARUSDT', 'PENDLEUSD': 'PENDLEUSDT', 'INJUSD': 'INJUSDT',
        'ONDOUSD': 'ONDOUSDT',
    }
    symbol = symbol_map.get(pair.upper(), pair.upper().replace('USD', 'USDT'))

    # REPLAY V2.2 ATUAL: mesmos TFs e mesma decision layer do Paper/Forward.
    # Mantemos pre-historia suficiente para formar swings/ATR/POIs antes do inicio
    # da janela auditada; somente os ts_corte dentro [inicio,fim] viram ciclos.
    mn_bruto  = _fetch_bybit_klines_historico(symbol, 'M',  3650, fim_ts_ms=fim_ts_ms)
    w1_bruto  = _fetch_bybit_klines_historico(symbol, 'W',   dias_historico + 900, fim_ts_ms=fim_ts_ms)
    d1_bruto  = _fetch_bybit_klines_historico(symbol, 'D',   dias_historico + 260, fim_ts_ms=fim_ts_ms)
    h4_bruto  = _fetch_bybit_klines_historico(symbol, '240', dias_historico + 120, fim_ts_ms=fim_ts_ms)
    h1_bruto  = _fetch_bybit_klines_historico(symbol, '60',  dias_historico + 35, fim_ts_ms=fim_ts_ms)
    m30_bruto = _fetch_bybit_klines_historico(symbol, '30',  dias_historico + 18, fim_ts_ms=fim_ts_ms)
    m15_bruto = _fetch_bybit_klines_historico(symbol, '15',  dias_historico + 9, fim_ts_ms=fim_ts_ms)
    m5_bruto  = _fetch_bybit_klines_historico(symbol, '5',   dias_historico + 4, fim_ts_ms=fim_ts_ms)
    m1_bruto  = _fetch_bybit_klines_historico(symbol, '1',   dias_historico + 1, fim_ts_ms=fim_ts_ms)

    mn, val_mn   = _validar_e_limpar_candles(mn_bruto, 'M')
    w1, val_w1   = _validar_e_limpar_candles(w1_bruto, 'W')
    d1, val_d1   = _validar_e_limpar_candles(d1_bruto, 'D')
    h4, val_h4   = _validar_e_limpar_candles(h4_bruto, '240')
    h1, val_h1   = _validar_e_limpar_candles(h1_bruto, '60')
    m30, val_m30 = _validar_e_limpar_candles(m30_bruto, '30')
    m15, val_m15 = _validar_e_limpar_candles(m15_bruto, '15')
    m5, val_m5   = _validar_e_limpar_candles(m5_bruto, '5')
    m1, val_m1   = _validar_e_limpar_candles(m1_bruto, '1')

    inicio_ts_ms = fim_ts_ms - dias_historico * 86400000

    MIN_M5_IDX = 60
    if len(m15) < 40 or len(m5) < MIN_M5_IDX + 20:
        return {'erro': f'dados insuficientes pra {pair} (M15={len(m15)}, M5={len(m5)})',
                'validacao_m15': val_m15, 'validacao_m5': val_m5}

    funil = {
        'total_ciclos_avaliados': 0, 'bias_ok': 0, 'zona_encontrada': 0,
        'zona_tipo_fvg': 0,
        'choch_confirmado': 0, 'choch_invalidado_antes_gatilho': 0,
        'sl_ok': 0, 'tp_ok': 0, 'sinais_validos_brutos': 0,
    }
    distribuicao_motivos = {}
    sinais_completos_brutos = []

    for i in range(MIN_M5_IDX, len(m5)):
        ts_corte = m5[i]['t']
        if ts_corte < inicio_ts_ms or ts_corte > fim_ts_ms:
            continue
        m5_ate_agora = m5[:i + 1]
        m15_ate_agora = [c for c in m15 if c['t'] <= ts_corte]
        d1_ate_agora = [c for c in d1 if c['t'] <= ts_corte]
        if len(m15_ate_agora) < 30:
            continue

        funil['total_ciclos_avaliados'] += 1
        tf_map = {
            'MN': [c for c in mn if c['t'] <= ts_corte],
            'W1': [c for c in w1 if c['t'] <= ts_corte],
            'D1': d1_ate_agora,
            'H4': [c for c in h4 if c['t'] <= ts_corte],
            'H1': [c for c in h1 if c['t'] <= ts_corte],
            'M30': [c for c in m30 if c['t'] <= ts_corte],
            'M15': m15_ate_agora,
            'M5': m5_ate_agora,
            'M1': [c for c in m1 if c['t'] <= ts_corte],
        }
        try:
            r = avaliar_vortex_decision_layer_v2(
                m15_ate_agora, m5_ate_agora, d1_ate_agora, candles_por_tf=tf_map,
                audit_pair=pair
            )
        except Exception as e:
            distribuicao_motivos[f'EXCECAO: {e}'] = distribuicao_motivos.get(f'EXCECAO: {e}', 0) + 1
            continue

        if r['bias'] in ('alta', 'baixa'):
            funil['bias_ok'] += 1
        if r['zone_top'] is not None:
            funil['zona_encontrada'] += 1
            if r['zone_type'] == 'FVG':
                funil['zona_tipo_fvg'] += 1
        if r['choch_confirmed']:
            funil['choch_confirmado'] += 1
        if r['failure_reason'] == 'CHOCH_INVALIDADO_ANTES_DO_GATILHO':
            funil['choch_invalidado_antes_gatilho'] += 1
        if r['sl'] is not None:
            funil['sl_ok'] += 1
        if r['tp'] is not None:
            funil['tp_ok'] += 1

        motivo_chave = 'SINAL_VALIDO' if r['valid'] else (r['failure_reason'] or 'MOTIVO_DESCONHECIDO')
        distribuicao_motivos[motivo_chave] = distribuicao_motivos.get(motivo_chave, 0) + 1

        if r['valid']:
            funil['sinais_validos_brutos'] += 1
            sinais_completos_brutos.append({**r, 'idx_m5': i, 'pair': pair})

    sinais_unicos = []
    chaves_vistas = set()
    for s in sinais_completos_brutos:
        chave = (s['choch_timestamp'], s['direction'], s['zone_type'])
        if chave not in chaves_vistas:
            chaves_vistas.add(chave)
            sinais_unicos.append(s)

    contagem_repeticoes = {}
    for s in sinais_completos_brutos:
        chave = (s['choch_timestamp'], s['direction'], s['zone_type'])
        contagem_repeticoes[chave] = contagem_repeticoes.get(chave, 0) + 1
    repeticoes_por_sinal_unico = [
        {'choch_timestamp': s['choch_timestamp'], 'direction': s['direction'], 'zone_type': s['zone_type'],
         'repeticoes': contagem_repeticoes[(s['choch_timestamp'], s['direction'], s['zone_type'])]}
        for s in sinais_unicos
    ]
    lista_repeticoes = [r['repeticoes'] for r in repeticoes_por_sinal_unico]
    auditoria_dedup = {
        'total_avaliacoes_brutas': len(sinais_completos_brutos),
        'total_sinais_estruturalmente_diferentes': len(sinais_unicos),
        'repeticoes_media_por_sinal': round(sum(lista_repeticoes) / len(lista_repeticoes), 2) if lista_repeticoes else None,
        'repeticoes_mediana_por_sinal': _percentil(sorted(lista_repeticoes), 50) if lista_repeticoes else None,
        'repeticoes_min': min(lista_repeticoes) if lista_repeticoes else None,
        'repeticoes_max': max(lista_repeticoes) if lista_repeticoes else None,
        'detalhe_por_sinal': repeticoes_por_sinal_unico,
        'nota': (
            'Cada "sinal único" já representa um CHoCH estruturalmente diferente (timestamp '
            'distinto). "repeticoes" = quantos ciclos M5 consecutivos esse MESMO CHoCH '
            'permaneceu válido (sem invalidação) até ser substituído ou expirado. Não é '
            'ruído/erro — é o tempo de vida causal de cada setup dentro do replay.'
        ),
    }

    sinais_long = [s for s in sinais_unicos if s['direction'] == 'LONG']
    sinais_short = [s for s in sinais_unicos if s['direction'] == 'SHORT']

    rrs = sorted(s['rr'] for s in sinais_unicos if s['rr'] is not None)

    def stats_rr(lista):
        if not lista:
            return None
        return {
            'n': len(lista), 'media': round(sum(lista) / len(lista), 3),
            'mediana': _percentil(lista, 50), 'min': lista[0], 'max': lista[-1],
            'p25': _percentil(lista, 25), 'p75': _percentil(lista, 75),
        }

    medicoes_mfe_mae = []
    for s in sinais_unicos:
        idx = s['idx_m5']
        entry = s['entry']
        direcao_mfe = 'alta' if s['direction'] == 'LONG' else 'baixa'
        candles_futuros = m5[idx + 1:]
        med = {'pair': pair, 'direction': s['direction'], 'rr': s['rr']}
        for j in janelas_mfe_mae:
            med[f'j{j}'] = _medir_mfe_mae_janela(candles_futuros, direcao_mfe, entry, j)
        medicoes_mfe_mae.append(med)

    mfe_mae_long = _agregar_mfe_mae([m for m in medicoes_mfe_mae if m['direction'] == 'LONG'], janelas_mfe_mae)
    mfe_mae_short = _agregar_mfe_mae([m for m in medicoes_mfe_mae if m['direction'] == 'SHORT'], janelas_mfe_mae)
    mfe_mae_global = _agregar_mfe_mae(medicoes_mfe_mae, janelas_mfe_mae)

    return {
        'pair': pair, 'dias_historico': dias_historico,
        'cohort': 'REPLAY', 'strategy_variant': PAPER_TRADING_V2_STRATEGY_VARIANT,
        'versao_pipeline': VORTEX_DECISION_LAYER_V2_VERSAO,
        'janela_fixa': {
            'data_inicio_ts_ms': inicio_ts_ms,
            'data_fim_ts_ms': fim_ts_ms,
            'data_inicio_iso': datetime.fromtimestamp(inicio_ts_ms / 1000, tz=timezone.utc).isoformat(),
            'data_fim_iso': datetime.fromtimestamp(fim_ts_ms / 1000, tz=timezone.utc).isoformat(),
            'quantidade_candles_m5': len(m5),
            'quantidade_candles_m15': len(m15),
            'quantidade_candles_d1': len(d1),
            'quantidade_candles_h1': len(h1), 'quantidade_candles_h4': len(h4),
            'quantidade_candles_w1': len(w1), 'quantidade_candles_mn': len(mn),
            'nota': (
                'Janela histórica FIXA e reproduzível — para repetir exatamente este período '
                'numa chamada futura, passe fim_ts_ms=' + str(fim_ts_ms) + ' explicitamente.'
            ),
        },
        'nota_metodologica': (
            'REPLAY SOMENTE AUDITORIA — MESMA avaliar_vortex_decision_layer_v2() V2.2 usada pelo Paper/Forward, '
            'com mapa MN/W1/D1/H4/H1/M30/M15/M5/M1 truncado causalmente. Não altera CHoCH, FVG, Premium/Discount, '
            'SL/TP existentes, gates ou motores legados removidos. Mesma '
            'metodologia causal já aprovada — cada ciclo só enxerga candles com t <= ts_corte. '
            'Sinais deduplicados por (choch_timestamp, direction, zone_type) — o mesmo CHoCH pode '
            'permanecer "válido" em vários ciclos M5 consecutivos até ser invalidado.'
        ),
        'validacao_dados': {'MN': val_mn, 'W1': val_w1, 'D1': val_d1, 'H4': val_h4, 'H1': val_h1, 'M30': val_m30, 'M15': val_m15, 'M5': val_m5, 'M1': val_m1},
        'funil': funil,
        'distribuicao_motivos_todos_ciclos': distribuicao_motivos,
        'total_sinais_unicos': len(sinais_unicos),
        'auditoria_dedup': auditoria_dedup,
        'sinais_long': len(sinais_long), 'sinais_short': len(sinais_short),
        'distribuicao_rr': {
            'global': stats_rr(rrs), 'LONG': stats_rr(sorted(s['rr'] for s in sinais_long if s['rr'] is not None)),
            'SHORT': stats_rr(sorted(s['rr'] for s in sinais_short if s['rr'] is not None)),
        },
        'exemplos_sinais_completos': sinais_unicos[:10],
        'mfe_mae_causal': {'global': mfe_mae_global, 'LONG': mfe_mae_long, 'SHORT': mfe_mae_short},
        'sinais_unicos_completos': sinais_unicos,
        'm5_completo': m5,
    }


def replay_vortex_decision_layer_v2_todos_pares(dias_historico=7, pares=None, fim_ts_ms=None):
    """Roda replay_vortex_decision_layer_v2() (sem alteração) pra cada
    par, agrega funil/RR/MFE-MAE globalmente. Erro num par não derruba
    os demais."""
    pares = pares or PARES_MONITORADOS_REPLAY
    resultados_por_pair = {}
    pares_com_erro = []

    for p in pares:
        try:
            r = replay_vortex_decision_layer_v2(p, dias_historico=dias_historico, fim_ts_ms=fim_ts_ms)
        except Exception as e:
            r = {'erro': str(e)}
        resultados_por_pair[p] = r
        if 'erro' in r:
            pares_com_erro.append(p)

    todos_sinais_unicos = []
    funil_agregado = {}
    for p, r in resultados_por_pair.items():
        if 'erro' in r:
            continue
        for k, v in r.get('funil', {}).items():
            funil_agregado[k] = funil_agregado.get(k, 0) + v
        todos_sinais_unicos.extend(r.get('sinais_unicos_completos', []))

    sinais_long_g = [s for s in todos_sinais_unicos if s['direction'] == 'LONG']
    sinais_short_g = [s for s in todos_sinais_unicos if s['direction'] == 'SHORT']
    rrs_g = sorted(s['rr'] for s in todos_sinais_unicos if s['rr'] is not None)

    def stats_rr(lista):
        if not lista:
            return None
        return {
            'n': len(lista), 'media': round(sum(lista) / len(lista), 3),
            'mediana': _percentil(lista, 50), 'min': lista[0], 'max': lista[-1],
        }

    return {
        'dias_historico': dias_historico, 'pares_testados': pares, 'pares_com_erro': pares_com_erro,
        'benchmark_principal': 'BTCUSD',
        'funil_agregado': funil_agregado,
        'total_sinais_unicos_global': len(todos_sinais_unicos),
        'sinais_long_global': len(sinais_long_g), 'sinais_short_global': len(sinais_short_g),
        'distribuicao_rr_global': {
            'global': stats_rr(rrs_g),
            'LONG': stats_rr(sorted(s['rr'] for s in sinais_long_g if s['rr'] is not None)),
            'SHORT': stats_rr(sorted(s['rr'] for s in sinais_short_g if s['rr'] is not None)),
        },
        'exemplos_sinais_completos_global': todos_sinais_unicos[:15],
        'nota_metodologica': (
            'Cada par processado de forma totalmente independente, chamando '
            'replay_vortex_decision_layer_v2() sem nenhuma alteração. Erro num par não '
            'derruba os demais. Para MFE/MAE e funil detalhado por par, ver '
            'resultados_por_pair[PAR] — cada um já traz o bloco completo.'
        ),
        'resultados_por_pair': resultados_por_pair,
    }


def init_paper_trading_v2_db(db_file):
    """Cria a tabela de paper trading v2, se não existir. Auto-blindada
    — não depende de init no boot do app.py. Tabela PRÓPRIA e ISOLADA,
    nunca compartilhada com scalp_replay_jobs, live_signals ou
    qualquer tabela de produção existente. UNIQUE constraint garante
    deduplicação a nível de banco — sobrevive a reinício, tick
    duplicado, candle reprocessado."""
    try:
        with sqlite3.connect(db_file) as conn:
            conn.execute('''
                CREATE TABLE IF NOT EXISTS paper_trading_v2_sinais (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    pair TEXT NOT NULL,
                    direction TEXT NOT NULL,
                    choch_timestamp INTEGER NOT NULL,
                    zone_type TEXT NOT NULL,
                    zone_source TEXT,
                    zone_top REAL, zone_bottom REAL,
                    choch_level REAL,
                    entry REAL, sl REAL, tp REAL, rr REAL,
                    tp_origem TEXT, sl_regra TEXT, reason TEXT,
                    candle_confirmacao_ts INTEGER,
                    detectado_em INTEGER,
                    status TEXT DEFAULT 'PENDING',
                    resultado_timestamp INTEGER,
                    candles_ate_evento INTEGER,
                    r_obtido REAL, mfe_pct REAL, mae_pct REAL,
                    spread_no_sinal TEXT DEFAULT 'NAO_MEDIDO',
                    telemetria_liquidity TEXT,
                    updated_at INTEGER,
                    UNIQUE(pair, choch_timestamp, direction, zone_type)
                )
            ''')
            conn.execute('''
                CREATE INDEX IF NOT EXISTS idx_paper_v2_status ON paper_trading_v2_sinais(status)
            ''')
            conn.execute('''
                CREATE INDEX IF NOT EXISTS idx_paper_v2_pair ON paper_trading_v2_sinais(pair)
            ''')
            conn.commit()
    except Exception as e:
        print(f"[paper_trading_v2] erro ao criar tabela: {e}")

    _migrar_colunas_notificacao_paper_v2(db_file)
    _migrar_coluna_telemetria_liquidity_paper_v2(db_file)
    _migrar_colunas_cohort_paper_v2(db_file)


def _migrar_coluna_telemetria_liquidity_paper_v2(db_file):
    """
    Migração aditiva e idempotente da telemetria V2.1.
    Não recria a tabela, não apaga nem altera sinais históricos.
    """
    try:
        with sqlite3.connect(db_file) as conn:
            colunas = {
                row[1]
                for row in conn.execute("PRAGMA table_info(paper_trading_v2_sinais)").fetchall()
            }
            if not colunas:
                # A tabela ainda não existe; init_paper_trading_v2_db() cuidará da criação.
                return False
            if 'telemetria_liquidity' not in colunas:
                conn.execute(
                    'ALTER TABLE paper_trading_v2_sinais '
                    'ADD COLUMN telemetria_liquidity TEXT'
                )
                conn.commit()
                print('[paper_trading_v2] migração OK: coluna telemetria_liquidity adicionada')
            return True
    except Exception as e:
        print(f'[paper_trading_v2] erro na migração telemetria_liquidity: {e}')
        return False


def _migrar_colunas_cohort_paper_v2(db_file):
    """Migração aditiva/idempotente REPLAY x FORWARD. Não altera decisões."""
    try:
        with sqlite3.connect(db_file) as conn:
            cols = {row[1] for row in conn.execute("PRAGMA table_info(paper_trading_v2_sinais)").fetchall()}
            if not cols:
                return False
            if 'cohort' not in cols:
                conn.execute("ALTER TABLE paper_trading_v2_sinais ADD COLUMN cohort TEXT")
            if 'strategy_variant' not in cols:
                conn.execute("ALTER TABLE paper_trading_v2_sinais ADD COLUMN strategy_variant TEXT")
            conn.execute("""
                UPDATE paper_trading_v2_sinais
                SET cohort = CASE
                    WHEN COALESCE(candle_confirmacao_ts, choch_timestamp) >= ? THEN 'FORWARD'
                    ELSE 'REPLAY'
                END
                WHERE cohort IS NULL OR cohort = ''
            """, (PAPER_TRADING_V2_FORWARD_WATERMARK_MS,))
            conn.execute("""
                UPDATE paper_trading_v2_sinais SET strategy_variant = ?
                WHERE strategy_variant IS NULL OR strategy_variant = ''
            """, (PAPER_TRADING_V2_STRATEGY_VARIANT,))
            conn.execute("CREATE INDEX IF NOT EXISTS idx_paper_v2_cohort ON paper_trading_v2_sinais(cohort)")
            conn.commit()
        return True
    except Exception as e:
        print(f"[paper_trading_v2] erro na migração cohort: {e}")
        return False


def _paper_v2_classificar_cohort(candle_confirmacao_ts):
    try:
        ts = int(candle_confirmacao_ts or 0)
    except Exception:
        ts = 0
    return 'FORWARD' if ts >= PAPER_TRADING_V2_FORWARD_WATERMARK_MS else 'REPLAY'


def _migrar_colunas_notificacao_paper_v2(db_file):
    """
    Migração ADITIVA, idempotente — adiciona as colunas de controle de
    notificação (notificado_novo_sinal, notificado_resultado) via
    ALTER TABLE, se ainda não existirem. IMPORTANTE: na primeira vez
    que as colunas são criadas (e SÓ nessa vez — detectado pelo
    ALTER TABLE ter tido sucesso, não falhado por coluna já existir),
    faz um BACKFILL marcando TODOS os sinais já existentes na tabela
    como já notificados (notificado_novo_sinal=1, notificado_resultado=1)
    — isso garante que sinais históricos (ex: os 67 já gravados antes
    desta feature existir) NUNCA disparam notificação retroativa como
    se fossem novos, satisfazendo a exigência explícita do ticket.
    Chamadas subsequentes (colunas já existem) não fazem nada.
    """
    coluna_foi_criada_agora = False
    try:
        with sqlite3.connect(db_file) as conn:
            conn.execute('ALTER TABLE paper_trading_v2_sinais ADD COLUMN notificado_novo_sinal INTEGER DEFAULT 0')
            conn.commit()
            coluna_foi_criada_agora = True
    except Exception:
        pass  # coluna já existe — normal em toda chamada depois da primeira

    try:
        with sqlite3.connect(db_file) as conn:
            conn.execute('ALTER TABLE paper_trading_v2_sinais ADD COLUMN notificado_resultado INTEGER DEFAULT 0')
            conn.commit()
    except Exception:
        pass

    if coluna_foi_criada_agora:
        try:
            with sqlite3.connect(db_file) as conn:
                conn.execute('''
                    UPDATE paper_trading_v2_sinais SET notificado_novo_sinal=1, notificado_resultado=1
                ''')
                conn.commit()
                print("[paper_trading_v2] migração de notificação: sinais históricos marcados como já notificados (evita spam retroativo)")
        except Exception as e:
            print(f"[paper_trading_v2] erro no backfill de notificação: {e}")



def _garantir_tabela_prealerta_paper_v2(db_file):
    """Estado mínimo e persistente do pré-alerta. Não toca na tabela de sinais."""
    try:
        with sqlite3.connect(db_file) as conn:
            conn.execute("""
                CREATE TABLE IF NOT EXISTS paper_trading_v2_prealertas (
                    setup_key TEXT PRIMARY KEY,
                    pair TEXT NOT NULL,
                    direction TEXT,
                    liquidity_tf TEXT,
                    liquidity_type TEXT,
                    sweep_level REAL,
                    first_capture_ts INTEGER,
                    sweep_confirm_ts INTEGER,
                    choch_timestamp INTEGER,
                    choch_level REAL,
                    zone_type TEXT,
                    zone_top REAL,
                    zone_bottom REAL,
                    limit_price REAL,
                    sl_ref REAL,
                    tp_ref REAL,
                    rr_ref REAL,
                    criado_em INTEGER NOT NULL
                )
            """)
            conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_paper_v2_prealert_pair "
                "ON paper_trading_v2_prealertas(pair, criado_em)"
            )
            conn.commit()
        return True
    except Exception as e:
        print(f"[paper_v2_prealert] erro ao garantir tabela: {e}")
        return False


def _paper_v2_prealert_setup_key(pair, r):
    """Identidade causal: novo sweep/estrutura/zona = novo setup; ticks repetidos = mesma chave."""
    parts = [
        pair, r.get('direction'), r.get('liquidity_tf'), r.get('liquidity_type'),
        r.get('sweep_level'), r.get('first_capture_ts'), r.get('sweep_confirm_ts'),
        r.get('choch_timestamp'), r.get('choch_level'), r.get('zone_type'),
        r.get('zone_top'), r.get('zone_bottom'),
    ]
    raw = "|".join("" if x is None else str(x) for x in parts)
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def _formatar_mensagem_prealerta_paper_v2(pair, r):
    ts_ms = r.get('choch_timestamp') or r.get('sweep_confirm_ts')
    ts_str = (
        datetime.fromtimestamp(ts_ms / 1000, tz=timezone.utc).strftime('%Y-%m-%d %H:%M UTC')
        if ts_ms else 'N/A'
    )
    mom = r.get('momentum_z')
    mom_txt = f"{mom:+.3f}Z" if isinstance(mom, (int, float)) else "N/A"
    return (
        f"🔥 <b>KAIROS — SETUP ARMADO / LIMIT MENTAL</b>\n"
        f"Par: {pair}\n"
        f"Direção: {r.get('direction')}\n"
        f"Liquidez varrida: {r.get('liquidity_tf')} {r.get('liquidity_type')} @ {r.get('sweep_level')}\n"
        f"First capture: CONFIRMADO\n"
        f"CHoCH/MSS M15: {r.get('choch_level')}\n"
        f"Displacement: {mom_txt}\n"
        f"Zona: {r.get('zone_type')} {r.get('prealert_zone_tf') or 'M15'} "
        f"[{r.get('zone_bottom')} — {r.get('zone_top')}]\n"
        f"🎯 LIMIT mental (CE 50%): {r.get('prealert_limit')}\n"
        f"🛑 SL ref.: {r.get('prealert_sl')}\n"
        f"🏁 TP1 obstáculo/parcial: {r.get('prealert_tp1') or 'N/A'}\n"
        f"R:R TP1: {r.get('prealert_tp1_rr') if r.get('prealert_tp1') is not None else 'N/A'}\n"
        f"🎯 TP2 liquidez estrutural: {r.get('prealert_tp2') or r.get('prealert_tp')}\n"
        f"R:R TP2: {r.get('prealert_tp2_rr') or r.get('prealert_rr')}\n"
        f"Origem TP2: {r.get('prealert_tp2_origem') or r.get('prealert_tp_origem')}\n"
        f"Horário estrutura: {ts_str}\n"
        f"⏳ AGUARDANDO RETESTE — A LIMIT AINDA NÃO FOI PREENCHIDA.\n"
        f"⚠️ Pré-alerta experimental/paper; não é ordem real."
    )


def _paper_v2_tentar_prealerta(db_file, pair, r, agora_ts_ms):
    """Envia UMA vez por setup causal. Reserva a chave no SQLite antes do Telegram para matar spam."""
    if r.get('failure_reason') != 'AGUARDANDO_RETESTE_ZONA':
        return False
    if r.get('prealert_limit') is None:
        return False
    if not _garantir_tabela_prealerta_paper_v2(db_file):
        return False

    setup_key = _paper_v2_prealert_setup_key(pair, r)
    try:
        with sqlite3.connect(db_file) as conn:
            cur = conn.execute("""
                INSERT OR IGNORE INTO paper_trading_v2_prealertas (
                    setup_key, pair, direction, liquidity_tf, liquidity_type,
                    sweep_level, first_capture_ts, sweep_confirm_ts,
                    choch_timestamp, choch_level, zone_type, zone_top, zone_bottom,
                    limit_price, sl_ref, tp_ref, rr_ref, criado_em
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """, (
                setup_key, pair, r.get('direction'), r.get('liquidity_tf'), r.get('liquidity_type'),
                r.get('sweep_level'), r.get('first_capture_ts'), r.get('sweep_confirm_ts'),
                r.get('choch_timestamp'), r.get('choch_level'), r.get('zone_type'),
                r.get('zone_top'), r.get('zone_bottom'), r.get('prealert_limit'),
                r.get('prealert_sl'), r.get('prealert_tp'), r.get('prealert_rr'), agora_ts_ms,
            ))
            conn.commit()
            novo = cur.rowcount > 0
        if not novo:
            return False

        ok = _paper_trading_v2_enviar_telegram(_formatar_mensagem_prealerta_paper_v2(pair, r))
        if ok:
            print(
                f"[paper_v2_prealert] ENVIADO {pair} key={setup_key[:12]} "
                f"limit={r.get('prealert_limit')} zone={r.get('zone_type')}"
            )
            if str(r.get('zone_type') or '').startswith('IFVG'):
                print(
                    f"[paper_v2_ifvg_audit] {pair} type={r.get('zone_type')} "
                    f"bottom={r.get('zone_bottom')} top={r.get('zone_top')} "
                    f"origin_ts={r.get('zone_origin_ts')} created_ts={r.get('zone_created_ts')} "
                    f"flip_ts={r.get('zone_flip_ts')} A={r.get('zone_source_a')} "
                    f"MID={r.get('zone_source_mid')} C={r.get('zone_source_c')} "
                    f"FLIP={r.get('zone_flip_candle')}"
                )
        else:
            print(f"[paper_v2_prealert] Telegram não confirmou envio {pair} key={setup_key[:12]}")
        return ok
    except Exception as e:
        print(f"[paper_v2_prealert] erro em {pair}: {e}")
        return False


def _paper_trading_v2_enviar_telegram(mensagem):
    """
    Envio de notificação Telegram — SECUNDÁRIO, nunca pode derrubar o
    paper trading. Qualquer falha (token ausente, rede fora, API
    Telegram indisponível) é engolida e logada, NUNCA propagada.
    Token/chat_id lidos de variável de ambiente — nunca hardcoded.
    Prioriza PAPER_TRADING_TELEGRAM_TOKEN/PAPER_TRADING_TELEGRAM_CHAT_ID
    (canal dedicado, recomendado — evita misturar teste com alertas
    reais de produção); se não configurados, cai para
    TELEGRAM_TOKEN/TELEGRAM_CHAT_ID (mesmo canal já usado em produção
    — só usar se você realmente quiser o paper misturado com alertas
    reais, o que normalmente NÃO é recomendado).
    """
    token = os.environ.get('PAPER_TRADING_TELEGRAM_TOKEN') or os.environ.get('TELEGRAM_TOKEN')
    chat_id = os.environ.get('PAPER_TRADING_TELEGRAM_CHAT_ID') or os.environ.get('TELEGRAM_CHAT_ID')
    if not token or not chat_id:
        return False
    try:
        requests.post(
            f'https://api.telegram.org/bot{token}/sendMessage',
            json={'chat_id': chat_id, 'text': mensagem, 'parse_mode': 'HTML'},
            timeout=8,
        )
        return True
    except Exception as e:
        print(f"[paper_trading_v2] Telegram indisponível, sinal continua salvo normalmente: {e}")
        return False


def _formatar_mensagem_novo_sinal_paper_v2(pair, sinal, cohort=None):
    ts_str = datetime.fromtimestamp(sinal['choch_timestamp'] / 1000, tz=timezone.utc).strftime('%Y-%m-%d %H:%M UTC')
    cohort = cohort or sinal.get('cohort') or _paper_v2_classificar_cohort(sinal.get('timestamp') or sinal.get('candle_confirmacao_ts'))
    titulo = "🟢 <b>FORWARD — NOVO SINAL</b>" if cohort == 'FORWARD' else "🔵 <b>REPLAY/HISTÓRICO — SINAL</b>"
    return (
        f"{titulo}\n"
        f"Par: {pair}\n"
        f"Direção: {sinal['direction']}\n"
        f"Timestamp: {ts_str}\n"
        f"Entry: {sinal['entry']}\n"
        f"SL: {sinal['sl']}\n"
        f"TP1 obstáculo/parcial: {sinal.get('tp1') or 'N/A'}\n"
        f"TP2 final: {sinal['tp']}\n"
        f"R:R final: {sinal['rr']}\n"
        f"Origem TP2: {sinal['tp_origem']}\n"
        f"Estado: PENDING\n"
        f"⚠️ 100% experimental — paper trading, zero dinheiro real."
    )


def _formatar_mensagem_resultado_paper_v2(sinal_row, resultado_status, r_obtido, ts_evento_ms):
    ts_str = datetime.fromtimestamp(ts_evento_ms / 1000, tz=timezone.utc).strftime('%Y-%m-%d %H:%M UTC') if ts_evento_ms else 'N/A'
    emoji = {'TP': '✅', 'SL': '❌', 'AMBIGUO': '⚠️', 'EXPIRED': '⌛'}.get(resultado_status, 'ℹ️')
    r_str = f"{r_obtido:+.2f}R" if r_obtido is not None else "N/A"
    try:
        cohort = sinal_row['cohort']
    except Exception:
        cohort = None
    if not cohort:
        try:
            cohort = _paper_v2_classificar_cohort(sinal_row['candle_confirmacao_ts'])
        except Exception:
            cohort = 'REPLAY'
    titulo = f"{emoji} <b>FORWARD — RESULTADO</b>" if cohort == 'FORWARD' else f"{emoji} <b>REPLAY/HISTÓRICO — RESULTADO</b>"
    return (
        f"{titulo}\n"
        f"Par: {sinal_row['pair']}\n"
        f"Direção: {sinal_row['direction']}\n"
        f"Entry: {sinal_row['entry']}\n"
        f"SL: {sinal_row['sl']}\n"
        f"TP: {sinal_row['tp']}\n"
        f"Resultado: {resultado_status}\n"
        f"R realizado: {r_str}\n"
        f"Horário da resolução: {ts_str}\n"
        f"⚠️ 100% experimental — paper trading, zero dinheiro real."
    )




def _paper_v2_diag_resumo(pair, r):
    """Telemetria compacta do MESMO Paper V2. Não altera decisão, banco,
    dedup ou Telegram; só expõe nos logs o encadeamento matemático usado.
    """
    try:
        ctx = r.get('context_bias') or {}
        mtf = r.get('mtf_summary') or {}
        liq_inside = r.get('liquidity_inside_zone') or []
        targets = r.get('next_liquidity_targets') or []
        obstacles = r.get('target_obstacles') or []
        alvo = targets[0] if targets else None
        obst = obstacles[0] if obstacles else None
        alvo_txt = 'N/A'
        obst_txt = 'N/A'
        if alvo:
            alvo_txt = f"{alvo.get('tf')}:{alvo.get('tipo')}@{alvo.get('nivel')} rr={alvo.get('rr')}"
        if obst:
            obst_txt = f"{obst.get('tf')}:{obst.get('tipo')}@{obst.get('nivel')} rr={obst.get('rr')}"
        tf_parts = []
        for tf in KAIROS_TF_ORDEM:
            d = mtf.get(tf)
            if not d:
                continue
            tf_parts.append(
                f"{tf}[piv={d.get('pivots',0)},eq={d.get('eq',0)},sw={d.get('sweeps',0)},z={d.get('zones',0)}]"
            )
        return (
            f"[paper_v2_diag] {pair} "
            f"valid={r.get('valid')} setup={r.get('setup_type')} dir={r.get('direction')} "
            f"ctx={ctx.get('final')} sweep={r.get('sweep_tf')}:{r.get('sweep_level')} ext={r.get('sweep_extreme')} "
            f"exec={r.get('execution_tf')} choch={r.get('choch_level')}@{r.get('choch_timestamp')} "
            f"momZ={r.get('momentum_z')} zone={r.get('zone_type')}[{r.get('zone_bottom')},{r.get('zone_top')}] "
            f"liq_inside={'SIM' if liq_inside else 'NAO'}({len(liq_inside)}) "
            f"entry={r.get('entry')} sl={r.get('sl')} sl_regra={r.get('sl_regra')} "
            f"tp={r.get('tp')} tp_origem={r.get('tp_origem')} rr={r.get('rr')} "
            f"next_liq={alvo_txt} first_obstacle={obst_txt} tp_final_liq={r.get('tp_final_liquidez')} "
            f"mtf={' '.join(tf_parts)}"
        )
    except Exception as e:
        return f"[paper_v2_diag] {pair} erro_formatando={e}"


def _paper_v2_diag_rejeicao(pair, r):
    """Linha curta para saber exatamente em que etapa um candidato morreu."""
    sl_audit = r.get('sl_audit') or {}
    sl_cands = sl_audit.get('candidatos') or []
    sl_txt = 'N/A'
    if sl_cands:
        sl_txt = ';'.join(
            f"{x.get('classe')}:{x.get('tf')}:{x.get('status')} ext={x.get('sweep_extreme')} sl={x.get('sl_buffered')}"
            for x in sl_cands[:3]
        )
    return (
        f"[paper_v2_reject] {pair} reason={r.get('failure_reason')} "
        f"ctx={(r.get('context_bias') or {}).get('final')} "
        f"sweep={r.get('sweep_tf')}:{r.get('sweep_level')} "
        f"exec={r.get('execution_tf')} choch={r.get('choch_level')} "
        f"momZ={r.get('momentum_z')} zone={r.get('zone_type')} "
        f"entry={r.get('entry')} sl_audit={sl_txt}"
    )


def paper_trading_v2_tick(pair, db_file, agora_ts_ms=None):
    """
    ÚNICO ponto de entrada por par. Busca candles recentes (janela
    curta — PAPER_TRADING_V2_JANELA_LOOKBACK_DIAS), reavalia os
    últimos ciclos M5 causalmente com avaliar_vortex_decision_layer_v2
    (SEM ALTERAÇÃO), grava (INSERT OR IGNORE — dedup garantida pelo
    UNIQUE constraint da tabela) qualquer sinal novo encontrado como
    PENDING, e resolve TP/SL/AMBIGUO dos sinais PENDING já existentes
    desse par usando _resolver_tp_sl_futuro (já existente, testada).
    Idempotente: rodar o mesmo tick 2x, ou reiniciar o processo, nunca
    duplica nem corrompe nada — o estado vive só no banco.
    NUNCA envia ordem pra nenhuma exchange — só lê candles públicos
    (mesmo _fetch_bybit_klines_historico já usado no replay) e grava
    na tabela própria.
    """
    # Garantia de schema no próprio caminho automático: deployments antigos
    # podem ter a tabela persistida no volume sem a coluna nova da V2.1.
    # Esta migração é aditiva/idempotente e preserva todo o histórico.
    _migrar_coluna_telemetria_liquidity_paper_v2(db_file)
    _migrar_colunas_cohort_paper_v2(db_file)

    if agora_ts_ms is None:
        agora_ts_ms = int(time.time() * 1000)

    symbol_map = {
        'BTCUSD': 'BTCUSDT', 'ETHUSD': 'ETHUSDT', 'SOLUSD': 'SOLUSDT', 'XRPUSD': 'XRPUSDT',
        'LINKUSD': 'LINKUSDT', 'ADAUSD': 'ADAUSDT', 'AVAXUSD': 'AVAXUSDT', 'BNBUSD': 'BNBUSDT',
        'AAVEUSD': 'AAVEUSDT', 'NEARUSD': 'NEARUSDT', 'PENDLEUSD': 'PENDLEUSDT', 'INJUSD': 'INJUSDT',
        'ONDOUSD': 'ONDOUSDT',
    }
    symbol = symbol_map.get(pair.upper(), pair.upper().replace('USD', 'USDT'))

    # Mesmo Paper V2; apenas ampliamos o mapa que alimenta a decisão.
    # Janelas escolhidas para manter candles suficientes aos pivôs/ATR sem
    # multiplicar paginação desnecessariamente.
    mn_bruto  = _fetch_bybit_klines_historico(symbol, 'M',  3650, fim_ts_ms=agora_ts_ms)
    w1_bruto  = _fetch_bybit_klines_historico(symbol, 'W',   900, fim_ts_ms=agora_ts_ms)
    d1_bruto  = _fetch_bybit_klines_historico(symbol, 'D',   260, fim_ts_ms=agora_ts_ms)
    h4_bruto  = _fetch_bybit_klines_historico(symbol, '240', 120, fim_ts_ms=agora_ts_ms)
    h1_bruto  = _fetch_bybit_klines_historico(symbol, '60',   35, fim_ts_ms=agora_ts_ms)
    m30_bruto = _fetch_bybit_klines_historico(symbol, '30',   18, fim_ts_ms=agora_ts_ms)
    m15_bruto = _fetch_bybit_klines_historico(symbol, '15',    9, fim_ts_ms=agora_ts_ms)
    m5_bruto  = _fetch_bybit_klines_historico(symbol, '5',     4, fim_ts_ms=agora_ts_ms)
    m1_bruto  = _fetch_bybit_klines_historico(symbol, '1',     1, fim_ts_ms=agora_ts_ms)

    mn, _  = _validar_e_limpar_candles(mn_bruto, 'M')
    w1, _  = _validar_e_limpar_candles(w1_bruto, 'W')
    d1, _  = _validar_e_limpar_candles(d1_bruto, 'D')
    h4, _  = _validar_e_limpar_candles(h4_bruto, '240')
    h1, _  = _validar_e_limpar_candles(h1_bruto, '60')
    m30, _ = _validar_e_limpar_candles(m30_bruto, '30')
    m15, _ = _validar_e_limpar_candles(m15_bruto, '15')
    m5, _  = _validar_e_limpar_candles(m5_bruto, '5')
    m1, _  = _validar_e_limpar_candles(m1_bruto, '1')

    novos_detectados = 0
    rejeicoes_diag = {}
    ultimo_rejeitado = None
    if len(m15) >= 40 and len(m5) >= 80:
        idx_inicio = max(60, len(m5) - 300)
        for i in range(idx_inicio, len(m5)):
            ts_corte = m5[i]['t']
            m5_ate_agora = m5[:i + 1]
            m15_ate_agora = [c for c in m15 if c['t'] <= ts_corte]
            d1_ate_agora = [c for c in d1 if c['t'] <= ts_corte]
            if len(m15_ate_agora) < 30:
                continue
            tf_map = {
                'MN': [c for c in mn if c['t'] <= ts_corte],
                'W1': [c for c in w1 if c['t'] <= ts_corte],
                'D1': d1_ate_agora,
                'H4': [c for c in h4 if c['t'] <= ts_corte],
                'H1': [c for c in h1 if c['t'] <= ts_corte],
                'M30': [c for c in m30 if c['t'] <= ts_corte],
                'M15': m15_ate_agora,
                'M5': m5_ate_agora,
                'M1': [c for c in m1 if c['t'] <= ts_corte],
            }
            try:
                r = avaliar_vortex_decision_layer_v2(
                    m15_ate_agora, m5_ate_agora, d1_ate_agora,
                    candles_por_tf=tf_map,
                    audit_pair=pair,
                )
            except Exception as e:
                print(f"[paper_trading_v2] erro na decisão MTF de {pair}: {e}")
                continue
            if not r['valid']:
                motivo = r.get('failure_reason') or 'DESCONHECIDO'
                rejeicoes_diag[motivo] = rejeicoes_diag.get(motivo, 0) + 1
                ultimo_rejeitado = r
                # PRÉ-ALERTA somente para o estado MAIS RECENTE do mercado.
                # Nunca envia os AGUARDANDO_RETESTE históricos percorridos pelo replay de 300 M5.
                if motivo == 'AGUARDANDO_RETESTE_ZONA' and i == len(m5) - 1:
                    try:
                        _paper_v2_tentar_prealerta(db_file, pair, r, agora_ts_ms)
                    except Exception as e_pre:
                        print(f"[paper_v2_prealert] falha isolada em {pair}: {e_pre}")
                continue
            # Telemetria paralela: não muda nenhuma decisão do Paper V2.
            try:
                telemetria_liquidity = _kairos_build_structural_liquidity_telemetry(tf_map, ts_corte, signal_result=r)
                telemetria_liquidity_json = json.dumps(telemetria_liquidity, ensure_ascii=False, separators=(',', ':'))
            except Exception as e_tel:
                print(f"[paper_trading_v2] telemetria estrutural indisponível em {pair}: {e_tel}")
                telemetria_liquidity_json = None
            try:
                with sqlite3.connect(db_file) as conn:
                    cursor = conn.execute('''
                        INSERT OR IGNORE INTO paper_trading_v2_sinais (
                            pair, direction, choch_timestamp, zone_type, zone_source,
                            zone_top, zone_bottom, choch_level, entry, sl, tp, rr,
                            tp_origem, sl_regra, reason, candle_confirmacao_ts,
                            detectado_em, status, spread_no_sinal, telemetria_liquidity, updated_at,
                            cohort, strategy_variant
                        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'PENDING', 'NAO_MEDIDO', ?, ?, ?, ?)
                    ''', (
                        pair, r['direction'], r['choch_timestamp'], r['zone_type'], r['zone_source'],
                        r['zone_top'], r['zone_bottom'], r['choch_level'], r['entry'], r['sl'], r['tp'], r['rr'],
                        r['tp_origem'], r['sl_regra'], r['reason'], r['timestamp'],
                        agora_ts_ms, telemetria_liquidity_json, agora_ts_ms,
                        _paper_v2_classificar_cohort(r['timestamp']), PAPER_TRADING_V2_STRATEGY_VARIANT,
                    ))
                    conn.commit()
                    if cursor.rowcount > 0:
                        novos_detectados += 1
                        print(_paper_v2_diag_resumo(pair, r))
                        cohort_sinal = _paper_v2_classificar_cohort(r['timestamp'])
                        print(f"[paper_v2_cohort] {pair} cohort={cohort_sinal} event_ts={r['timestamp']} watermark={PAPER_TRADING_V2_FORWARD_WATERMARK_MS}")
                        try:
                            _paper_trading_v2_enviar_telegram(_formatar_mensagem_novo_sinal_paper_v2(pair, r, cohort=cohort_sinal))
                        except Exception as e_tg:
                            print(f"[paper_trading_v2] erro ao notificar novo sinal de {pair}: {e_tg}")
                        try:
                            with sqlite3.connect(db_file) as conn2:
                                conn2.execute(
                                    "UPDATE paper_trading_v2_sinais SET notificado_novo_sinal=1 WHERE id=?",
                                    (cursor.lastrowid,),
                                )
                                conn2.commit()
                        except Exception as e_flag:
                            print(f"[paper_trading_v2] erro ao marcar notificado_novo_sinal de {pair}: {e_flag}")
            except Exception as e:
                print(f"[paper_trading_v2] erro ao gravar sinal de {pair}: {e}")

    # Uma linha agregada por par/tick: evita spam de centenas de candles rejeitados,
    # mas mostra exatamente onde a matemática está travando.
    if rejeicoes_diag:
        top_rej = sorted(rejeicoes_diag.items(), key=lambda kv: kv[1], reverse=True)[:6]
        print(f"[paper_v2_funnel] {pair} novos={novos_detectados} rejeicoes={dict(top_rej)}")
        if novos_detectados == 0 and ultimo_rejeitado is not None:
            print(_paper_v2_diag_rejeicao(pair, ultimo_rejeitado))

    resolvidos = 0
    try:
        with sqlite3.connect(db_file) as conn:
            conn.row_factory = sqlite3.Row
            pendentes = conn.execute(
                "SELECT * FROM paper_trading_v2_sinais WHERE pair=? AND status='PENDING'", (pair,)
            ).fetchall()

        for sinal in pendentes:
            idx_candle_entrada = None
            for j, c in enumerate(m5):
                if c['t'] == sinal['candle_confirmacao_ts']:
                    idx_candle_entrada = j
                    break
            if idx_candle_entrada is None:
                idade_dias = (agora_ts_ms - sinal['choch_timestamp']) / 86400000
                if idade_dias > PAPER_TRADING_V2_EXPIRACAO_DIAS:
                    with sqlite3.connect(db_file) as conn:
                        conn.execute(
                            "UPDATE paper_trading_v2_sinais SET status='EXPIRED', updated_at=? WHERE id=?",
                            (agora_ts_ms, sinal['id']),
                        )
                        conn.commit()
                    try:
                        _paper_trading_v2_enviar_telegram(
                            _formatar_mensagem_resultado_paper_v2(sinal, 'EXPIRED', None, agora_ts_ms)
                        )
                    except Exception as e_tg:
                        print(f"[paper_trading_v2] erro ao notificar EXPIRED de {pair}: {e_tg}")
                    try:
                        with sqlite3.connect(db_file) as conn2:
                            conn2.execute(
                                "UPDATE paper_trading_v2_sinais SET notificado_resultado=1 WHERE id=?", (sinal['id'],)
                            )
                            conn2.commit()
                    except Exception as e_flag:
                        print(f"[paper_trading_v2] erro ao marcar notificado_resultado (EXPIRED) de {pair}: {e_flag}")
                continue

            candles_futuros = m5[idx_candle_entrada + 1:]
            direcao_lower = 'alta' if sinal['direction'] == 'LONG' else 'baixa'
            res = _resolver_tp_sl_futuro(
                candles_futuros, direcao_lower, sinal['entry'], sinal['sl'], sinal['tp'], None,
                max_candles=len(candles_futuros),
            )
            evento = 'TP' if res['resultado'] == 'TP1' else res['resultado']

            if evento in ('TP', 'SL', 'AMBIGUO'):
                r_obtido = sinal['rr'] if evento == 'TP' else (-1.0 if evento == 'SL' else None)
                ts_evento = None
                if res['candles_ate_resolucao'] and res['candles_ate_resolucao'] - 1 < len(candles_futuros):
                    ts_evento = candles_futuros[res['candles_ate_resolucao'] - 1]['t']
                with sqlite3.connect(db_file) as conn:
                    conn.execute('''
                        UPDATE paper_trading_v2_sinais
                        SET status=?, resultado_timestamp=?, candles_ate_evento=?,
                            r_obtido=?, mfe_pct=?, mae_pct=?, updated_at=?
                        WHERE id=?
                    ''', (evento, ts_evento, res['candles_ate_resolucao'], r_obtido,
                          res['mfe_pct'], res['mae_pct'], agora_ts_ms, sinal['id']))
                    conn.commit()
                resolvidos += 1
                try:
                    _paper_trading_v2_enviar_telegram(
                        _formatar_mensagem_resultado_paper_v2(sinal, evento, r_obtido, ts_evento)
                    )
                except Exception as e_tg:
                    print(f"[paper_trading_v2] erro ao notificar resultado de {pair}: {e_tg}")
                try:
                    with sqlite3.connect(db_file) as conn2:
                        conn2.execute(
                            "UPDATE paper_trading_v2_sinais SET notificado_resultado=1 WHERE id=?", (sinal['id'],)
                        )
                        conn2.commit()
                except Exception as e_flag:
                    print(f"[paper_trading_v2] erro ao marcar notificado_resultado de {pair}: {e_flag}")
            else:
                idade_dias = (agora_ts_ms - sinal['choch_timestamp']) / 86400000
                if idade_dias > PAPER_TRADING_V2_EXPIRACAO_DIAS:
                    with sqlite3.connect(db_file) as conn:
                        conn.execute(
                            "UPDATE paper_trading_v2_sinais SET status='EXPIRED', updated_at=? WHERE id=?",
                            (agora_ts_ms, sinal['id']),
                        )
                        conn.commit()
                    try:
                        _paper_trading_v2_enviar_telegram(
                            _formatar_mensagem_resultado_paper_v2(sinal, 'EXPIRED', None, agora_ts_ms)
                        )
                    except Exception as e_tg:
                        print(f"[paper_trading_v2] erro ao notificar EXPIRED de {pair}: {e_tg}")
                    try:
                        with sqlite3.connect(db_file) as conn2:
                            conn2.execute(
                                "UPDATE paper_trading_v2_sinais SET notificado_resultado=1 WHERE id=?", (sinal['id'],)
                            )
                            conn2.commit()
                    except Exception as e_flag:
                        print(f"[paper_trading_v2] erro ao marcar notificado_resultado (EXPIRED) de {pair}: {e_flag}")
    except Exception as e:
        print(f"[paper_trading_v2] erro ao resolver pendentes de {pair}: {e}")

    return {'pair': pair, 'novos_detectados': novos_detectados, 'resolvidos_neste_tick': resolvidos}


def paper_trading_v2_tick_todos_pares(db_file, pares=None, agora_ts_ms=None):
    """Roda paper_trading_v2_tick() (sem alteração) pra cada par.
    Erro num par NUNCA derruba os demais nem qualquer outro caminho do
    sistema — cada par é isolado em seu próprio try/except."""
    pares = pares or PARES_MONITORADOS_REPLAY
    resultados = {}
    for p in pares:
        try:
            resultados[p] = paper_trading_v2_tick(p, db_file, agora_ts_ms=agora_ts_ms)
        except Exception as e:
            resultados[p] = {'pair': p, 'erro': str(e)}
    return resultados


def paper_trading_v2_relatorio(db_file):
    """Telemetria completa — SOMENTE LEITURA da tabela própria de
    paper trading. Nunca escreve, nunca chama V2, nunca envia ordem."""
    with sqlite3.connect(db_file) as conn:
        conn.row_factory = sqlite3.Row
        todos = conn.execute("SELECT * FROM paper_trading_v2_sinais ORDER BY choch_timestamp ASC").fetchall()
        todos = [dict(r) for r in todos]

    total = len(todos)
    pendentes = [s for s in todos if s['status'] == 'PENDING']
    tp = [s for s in todos if s['status'] == 'TP']
    sl = [s for s in todos if s['status'] == 'SL']
    ambiguo = [s for s in todos if s['status'] == 'AMBIGUO']
    expired = [s for s in todos if s['status'] == 'EXPIRED']

    resolvidos = tp + sl
    rs = [s['r_obtido'] for s in resolvidos if s['r_obtido'] is not None]
    win_rate = round(100 * len(tp) / len(resolvidos), 2) if resolvidos else None
    expectancy = round(sum(rs) / len(rs), 4) if rs else None
    mediana_r = _percentil(sorted(rs), 50) if rs else None

    def bloco_direcao(lista_status):
        r_dir = [s['r_obtido'] for s in lista_status if s['status'] in ('TP', 'SL') and s['r_obtido'] is not None]
        wins = sum(1 for s in lista_status if s['status'] == 'TP')
        losses = sum(1 for s in lista_status if s['status'] == 'SL')
        n_resolvido = wins + losses
        return {
            'sinais': len(lista_status), 'tp': wins, 'sl': losses,
            'wr': round(100 * wins / n_resolvido, 2) if n_resolvido else None,
            'expectancy': round(sum(r_dir) / len(r_dir), 4) if r_dir else None,
            'mediana_R': _percentil(sorted(r_dir), 50) if r_dir else None,
        }

    long_sinais = [s for s in todos if s['direction'] == 'LONG']
    short_sinais = [s for s in todos if s['direction'] == 'SHORT']

    por_par = {}
    for s in todos:
        por_par.setdefault(s['pair'], []).append(s)
    resultado_por_par = {p: bloco_direcao(lista) for p, lista in por_par.items()}

    cronologico = sorted([s for s in todos if s['status'] in ('TP', 'SL', 'AMBIGUO')], key=lambda s: s['choch_timestamp'])
    max_win, max_loss, cur_win, cur_loss = 0, 0, 0, 0
    for s in cronologico:
        if s['status'] == 'TP':
            cur_win += 1
            cur_loss = 0
        else:
            cur_loss += 1
            cur_win = 0
        max_win = max(max_win, cur_win)
        max_loss = max(max_loss, cur_loss)

    resultado_diario = {}
    for s in resolvidos:
        dia = datetime.fromtimestamp(s['choch_timestamp'] / 1000, tz=timezone.utc).strftime('%Y-%m-%d')
        resultado_diario.setdefault(dia, []).append(s['r_obtido'])
    resultado_diario_agregado = {
        dia: {'n': len(rs_dia), 'soma_R': round(sum(rs_dia), 3)} for dia, rs_dia in resultado_diario.items()
    }

    return {
        'total_sinais': total, 'pendentes': len(pendentes), 'tp': len(tp), 'sl': len(sl),
        'ambiguo': len(ambiguo), 'expired': len(expired),
        'win_rate_pct': win_rate, 'expectancy_R': expectancy, 'mediana_R': mediana_r,
        'max_win_streak': max_win, 'max_loss_streak': max_loss,
        'LONG': bloco_direcao(long_sinais), 'SHORT': bloco_direcao(short_sinais),
        'resultado_por_par': resultado_por_par,
        'resultado_diario': resultado_diario_agregado,
        'nota_spread': 'spread_no_sinal sempre NAO_MEDIDO — candles Bybit são OHLC, sem bid/ask disponível.',
        'nota_custos': 'Resultado 100% BRUTO — nenhum fee/slippage/funding foi simulado ou inventado.',
        'nota_metodologica': (
            'FORWARD TEST / PAPER TRADING — 100% experimental, ZERO dinheiro real, ZERO ordem '
            'enviada a qualquer exchange. Reaproveita avaliar_vortex_decision_layer_v2() e '
            '_resolver_tp_sl_futuro() SEM ALTERAÇÃO NENHUMA. Tabela isolada '
            '(paper_trading_v2_sinais), nunca compartilhada com produção.'
        ),
    }


@explicacao_bp.route("/scalp_gates_vortex/paper_trading_v2_tick", methods=["GET"])
def paper_trading_v2_tick_endpoint():
    """
    Roda paper_trading_v2_tick_todos_pares() SINCRONAMENTE (rápido —
    só janela curta de lookback, não replay histórico completo).
    Chamar periodicamente (ex: a cada fechamento de M5, via cron
    externo/Railway scheduled job) para manter o paper trading ativo.
    NUNCA envia ordem.

    PROTEGIDO por segredo — lido da variável de ambiente
    PAPER_TRADING_TICK_SECRET (nunca hardcoded, nunca versionado).
    Aceita o segredo via header 'X-Paper-Tick-Secret' (preferencial —
    não fica em logs de acesso nem em histórico de navegador) ou via
    query param '&token=' (fallback, pra serviços de cron gratuitos
    que só suportam URL simples, sem header customizado).

    Uso: ?confirm=RODAR_PAPER_TICK&token=<segredo>
    ou header: X-Paper-Tick-Secret: <segredo>

    FAIL-CLOSED: se PAPER_TRADING_TICK_SECRET não estiver configurado
    no ambiente, o endpoint recusa TODAS as chamadas (nunca abre
    acesso público por omissão).
    """
    segredo_configurado = os.environ.get('PAPER_TRADING_TICK_SECRET')
    if not segredo_configurado:
        return jsonify({
            "erro": "endpoint desabilitado — variável de ambiente PAPER_TRADING_TICK_SECRET não configurada",
            "como_resolver": "defina PAPER_TRADING_TICK_SECRET nas variáveis de ambiente do Railway antes de usar este endpoint",
        }), 503

    segredo_recebido = request.headers.get('X-Paper-Tick-Secret') or request.args.get('token')
    if not segredo_recebido or segredo_recebido != segredo_configurado:
        return jsonify({"erro": "não autorizado"}), 401

    if request.args.get('confirm') != 'RODAR_PAPER_TICK':
        return jsonify({
            "erro": "endpoint protegido contra chamada acidental",
            "como_usar": "adiciona &confirm=RODAR_PAPER_TICK na URL",
        }), 400

    db_file = _db_file_explicacao()
    init_paper_trading_v2_db(db_file)
    try:
        resultado = paper_trading_v2_tick_todos_pares(db_file)
        return jsonify({"status": "ok", "resultados_por_par": resultado})
    except Exception as e:
        return jsonify({"erro": f"erro no tick de paper trading: {e}"}), 500


@explicacao_bp.route("/scalp_gates_vortex/paper_trading_v2_relatorio", methods=["GET"])
def paper_trading_v2_relatorio_endpoint():
    """Telemetria completa do paper trading — somente leitura."""
    db_file = _db_file_explicacao()
    init_paper_trading_v2_db(db_file)
    try:
        return jsonify(paper_trading_v2_relatorio(db_file))
    except Exception as e:
        return jsonify({"erro": f"erro ao gerar relatório: {e}"}), 500


def paper_trading_v2_export_completo(db_file, limit=None, offset=None, desde_ts_ms=None):
    """
    Export somente-leitura de TODOS os registros crus da tabela
    paper_trading_v2_sinais — sem nenhuma agregação, sem nenhum
    cálculo, sem nenhuma transformação de dados (diferente de
    paper_trading_v2_relatorio(), que agrega estatísticas). Serve para
    autópsia manual (planilha/notebook externo) dos sinais já
    detectados/resolvidos pelo paper trading v2.

    Reaproveita a MESMA tabela paper_trading_v2_sinais já usada por
    paper_trading_v2_tick() e paper_trading_v2_relatorio() — não cria
    tabela nova, não altera nenhum registro (só SELECT, nunca UPDATE/
    INSERT/DELETE).

    Filtros OPCIONAIS — nenhum deles muda a natureza da query base
    ("SELECT * FROM paper_trading_v2_sinais ORDER BY choch_timestamp
    ASC"), só restringem o conjunto retornado:
    - desde_ts_ms: só sinais com choch_timestamp >= desde_ts_ms
    - limit: número máximo de linhas retornadas
    - offset: pula as N primeiras linhas (só tem efeito junto com limit)
    """
    with sqlite3.connect(db_file) as conn:
        conn.row_factory = sqlite3.Row
        query = "SELECT * FROM paper_trading_v2_sinais"
        params = []
        if desde_ts_ms is not None:
            query += " WHERE choch_timestamp >= ?"
            params.append(desde_ts_ms)
        query += " ORDER BY choch_timestamp ASC"
        if limit is not None:
            query += " LIMIT ?"
            params.append(limit)
            if offset is not None:
                query += " OFFSET ?"
                params.append(offset)
        rows = conn.execute(query, params).fetchall()
        sinais = [dict(r) for r in rows]

    return {"total": len(sinais), "sinais": sinais}


@explicacao_bp.route("/scalp_gates_vortex/paper_trading_v2_export", methods=["GET"])
def paper_trading_v2_export_endpoint():
    """
    Export somente-leitura de TODOS os registros crus da tabela
    paper_trading_v2_sinais, para autópsia manual dos sinais já
    detectados/resolvidos. NÃO altera nenhum registro (só SELECT).

    Mesmo padrão de autenticação de paper_trading_v2_tick_endpoint():
    segredo lido de PAPER_TRADING_TICK_SECRET (nunca hardcoded), aceito
    via header 'X-Paper-Tick-Secret' (preferencial) ou query param
    '&token=' (fallback). FAIL-CLOSED: se a variável de ambiente não
    estiver configurada, recusa TODAS as chamadas.

    Diferença deliberada em relação ao tick: este endpoint é somente
    leitura, então NÃO exige '&confirm=RODAR_PAPER_TICK' — essa trava
    existe no tick para evitar disparo acidental de um ciclo de
    detecção/gravação; aqui não há nada a disparar, só consulta.

    Uso: ?token=<segredo>
    ou header: X-Paper-Tick-Secret: <segredo>
    Opcional: &limit=500&offset=0&desde_ts_ms=1700000000000
    """
    segredo_configurado = os.environ.get('PAPER_TRADING_TICK_SECRET')
    if not segredo_configurado:
        return jsonify({
            "erro": "endpoint desabilitado — variável de ambiente PAPER_TRADING_TICK_SECRET não configurada",
            "como_resolver": "defina PAPER_TRADING_TICK_SECRET nas variáveis de ambiente do Railway antes de usar este endpoint",
        }), 503

    segredo_recebido = request.headers.get('X-Paper-Tick-Secret') or request.args.get('token')
    if not segredo_recebido or segredo_recebido != segredo_configurado:
        return jsonify({"erro": "não autorizado"}), 401

    limit_param = request.args.get('limit')
    offset_param = request.args.get('offset')
    desde_ts_ms_param = request.args.get('desde_ts_ms')

    limit = int(limit_param) if limit_param else None
    offset = int(offset_param) if offset_param else None
    desde_ts_ms = int(desde_ts_ms_param) if desde_ts_ms_param else None

    db_file = _db_file_explicacao()
    init_paper_trading_v2_db(db_file)
    try:
        resultado = paper_trading_v2_export_completo(db_file, limit=limit, offset=offset, desde_ts_ms=desde_ts_ms)
        return jsonify(resultado)
    except Exception as e:
        return jsonify({"erro": f"erro no export de paper trading: {e}"}), 500


# ═══════════════════════════════════════════════════════════════════════
# paper_trading_v2_diagnostico_bias — item aprovado do ticket. SOMENTE
# DIAGNÓSTICO/LEITURA — não escreve nada, não altera nenhuma tabela,
# não decide nada. Reaproveita EXCLUSIVAMENTE
# avaliar_vortex_decision_layer_v2() (SEM ALTERAÇÃO NENHUMA) sobre a
# MESMA janela/metodologia que paper_trading_v2_tick usa, mas em vez
# de gravar sinais, conta a distribuição de BIAS e, pra ciclos com
# BIAS=SHORT especificamente, em qual etapa cada avaliação parou —
# exatamente pra responder "existiu BIAS SHORT e, se sim, onde foi
# eliminado?" sem usar resultado futuro pra decidir nada (cada ciclo
# é avaliado isoladamente e causalmente, igual ao replay/paper já
# aprovados).
# ═══════════════════════════════════════════════════════════════════════

