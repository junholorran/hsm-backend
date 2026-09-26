# scalp_engine.py — KAIROS Paper V2.2 causal engine

import sqlite3
import hashlib
import time
import os
import requests
import json
from flask import Blueprint, jsonify, current_app, request
from datetime import datetime, timezone, timedelta

STOP_BUFFER_PCT = 0.001
ATR_BUFFER_MULT = 0.25

explicacao_bp = Blueprint('kairos_v2', __name__)
INTERVALO_MS_POR_LABEL = {'M': 2592000000, 'W': 604800000, 'D': 86400000, '240': 14400000, '60': 3600000, '30': 1800000, '15': 900000, '5': 300000, '1': 60000}


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
    swing_high_origin_ts = None
    swing_low_origin_ts = None
    swing_high_crossed = False
    swing_low_crossed = False
    bias = 'neutro'
    eventos = []

    # O estado de break e o estado do SL têm papéis diferentes:
    # - swing_*_level = nível estrutural ainda elegível para BOS/CHoCH;
    # - latest_* = último pivot Lux confirmado, mesmo que esse pivot já tenha
    #   sido quebrado antes. É este segundo estado que a execução precisa para
    #   saber qual era o swing oposto mais recente NO INSTANTE da quebra.
    latest_high_level = None
    latest_low_level = None
    latest_high_origin_ts = None
    latest_low_origin_ts = None

    for i in range(swing_size + 1, n):
        if legs[i] != legs[i - 1]:
            idx_pivot = i - swing_size
            if idx_pivot < 0:
                continue
            if legs[i] == 1:
                swing_low_level = candles[idx_pivot]['l']
                swing_low_origin_ts = candles[idx_pivot]['t']
                swing_low_crossed = False
                latest_low_level = swing_low_level
                latest_low_origin_ts = swing_low_origin_ts
            else:
                swing_high_level = candles[idx_pivot]['h']
                swing_high_origin_ts = candles[idx_pivot]['t']
                swing_high_crossed = False
                latest_high_level = swing_high_level
                latest_high_origin_ts = swing_high_origin_ts

        c = candles[i]
        if swing_high_level is not None and not swing_high_crossed and c['c'] > swing_high_level:
            tipo = 'CHoCH' if bias == 'baixa' else 'BOS'
            eventos.append({
                'tipo': tipo, 'direcao': 'alta', 'nivel': swing_high_level,
                'broken_swing_origin_ts': swing_high_origin_ts,
                'protected_swing_type': 'LOW',
                'protected_swing_level': latest_low_level,
                'protected_swing_origin_ts': latest_low_origin_ts,
                't': c['t'], 'index': i
            })
            bias = 'alta'
            swing_high_crossed = True
        if swing_low_level is not None and not swing_low_crossed and c['c'] < swing_low_level:
            tipo = 'CHoCH' if bias == 'alta' else 'BOS'
            eventos.append({
                'tipo': tipo, 'direcao': 'baixa', 'nivel': swing_low_level,
                'broken_swing_origin_ts': swing_low_origin_ts,
                'protected_swing_type': 'HIGH',
                'protected_swing_level': latest_high_level,
                'protected_swing_origin_ts': latest_high_origin_ts,
                't': c['t'], 'index': i
            })
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


def aplicar_buffer_stop(nivel, direcao, buffer_pct=STOP_BUFFER_PCT):
    if direcao == 'alta':
        return nivel * (1 - buffer_pct)
    return nivel * (1 + buffer_pct)



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


def _db_file_explicacao():
    return current_app.config.get('DB_FILE') or current_app.config.get('DB_PATH', '/data/alerts.db')


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


def _kairos_candle_close_ts(open_ts, interval_label):
    """Fecho causal do kline Bybit. Mês usa calendário UTC real, não 30 dias fixos."""
    if interval_label == 'M':
        dt=datetime.fromtimestamp(open_ts / 1000, tz=timezone.utc)
        if dt.month == 12:
            nxt=datetime(dt.year + 1, 1, 1, tzinfo=timezone.utc)
        else:
            nxt=datetime(dt.year, dt.month + 1, 1, tzinfo=timezone.utc)
        return int(nxt.timestamp() * 1000)
    dur=INTERVALO_MS_POR_LABEL.get(interval_label)
    return open_ts + dur if dur is not None else None


def _kairos_candles_fechados_ate(candles, interval_label, cutoff_ts):
    """Snapshot causal: só devolve candles cujo FECHO já era conhecido no cutoff.

    Bybit usa timestamp de ABERTURA em kline.t. Portanto c['t'] <= cutoff
    não basta: o OHLC final só pode entrar quando o candle fechou.
    """
    if not candles:
        return []
    out=[]
    for candle in candles:
        ts=candle.get('t')
        close_ts=_kairos_candle_close_ts(ts, interval_label) if ts is not None else None
        if close_ts is not None and close_ts <= cutoff_ts:
            out.append(candle)
    return out


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
    intervalo_ms = {'M': 2592000000, 'W': 604800000, 'D': 86400000, '240': 14400000, '60': 3600000, '30': 1800000, '15': 900000, '5': 300000, '1': 60000}.get(interval, 900000)
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



def _resolver_tp_sl_futuro(candles_gatilho_futuros, direcao, entry, sl, tp1, tp2, max_candles):
    """
    Cálculo causal de momentum: anda
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



def _resolver_gestao_2r_3r_be(candles_futuros, direction, entry, sl, tp2, max_candles):
    """Resolve o plano operacional do Kairos sem inventar ordem intrabar.

    Antes de 2R: SL original continua válido.
    Ao tocar 2R: parcial é considerada atingida e, a partir do candle
    SEGUINTE, o stop do restante vai para BE (entry).
    TP final: 3R. Se 2R e SL original aparecem no mesmo candle, AMBIGUO.
    Se 2R e 3R aparecem no mesmo candle sem SL original, TP.
    """
    janela = candles_futuros[:max_candles]
    if not janela:
        return {'resultado':'NENHUM','candles_ate_resolucao':None,'mfe_pct':0.0,'mae_pct':0.0}

    risk = abs(entry - sl)
    if risk <= 0:
        return {'resultado':'AMBIGUO','candles_ate_resolucao':None,'mfe_pct':0.0,'mae_pct':0.0}
    sign = 1.0 if direction == 'LONG' else -1.0
    tp1 = entry + sign * 2.0 * risk
    be_ativo = False
    mfe = 0.0
    mae = 0.0

    for idx, candle in enumerate(janela):
        if direction == 'LONG':
            mfe = max(mfe, candle['h'] - entry)
            mae = max(mae, entry - candle['l'])
            hit_tp1 = candle['h'] >= tp1
            hit_tp2 = candle['h'] >= tp2
            hit_sl_original = candle['l'] <= sl
            hit_be = be_ativo and candle['l'] <= entry
        else:
            mfe = max(mfe, entry - candle['l'])
            mae = max(mae, candle['h'] - entry)
            hit_tp1 = candle['l'] <= tp1
            hit_tp2 = candle['l'] <= tp2
            hit_sl_original = candle['h'] >= sl
            hit_be = be_ativo and candle['h'] >= entry

        stats = {
            'candles_ate_resolucao': idx + 1,
            'mfe_pct': round(mfe / entry * 100, 4),
            'mae_pct': round(mae / entry * 100, 4),
        }

        if not be_ativo:
            if hit_sl_original and (hit_tp1 or hit_tp2):
                return {'resultado':'AMBIGUO', **stats}
            if hit_tp2:
                return {'resultado':'TP', **stats}
            if hit_sl_original:
                return {'resultado':'SL', **stats}
            if hit_tp1:
                be_ativo = True
                continue
        else:
            if hit_tp2 and hit_be:
                return {'resultado':'AMBIGUO', **stats}
            if hit_tp2:
                return {'resultado':'TP', **stats}
            if hit_be:
                return {'resultado':'BE', **stats}

    return {
        'resultado':'NENHUM','candles_ate_resolucao':None,
        'mfe_pct':round(mfe / entry * 100,4),'mae_pct':round(mae / entry * 100,4),
    }

JANELAS_MFE_MAE_PADRAO = (20, 50)


def _medir_mfe_mae_janela(candles_futuros, direcao, entry, janela):
    """
    Mede MFE/MAE numa única janela, reaproveitando exatamente a mesma
    fórmula causal compartilhada (não duplicada
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


KAIROS_TF_ORDEM = ('MN', 'W1', 'D1', 'H4', 'H1', 'M30', 'M15', 'M5', 'M1')
KAIROS_TF_PESO = {'MN': 9, 'W1': 8, 'D1': 7, 'H4': 6, 'H1': 5, 'M30': 4, 'M15': 3, 'M5': 2, 'M1': 1}
KAIROS_SWEEP_LEFT = 20
KAIROS_SWEEP_RIGHT = 20
KAIROS_SWEEP_CONFIRM_BARS = 3


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
    """FVG/IFVG com geometria ICT canônica de 3 candles e ciclo de vida operacional.

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
        if atual['l'] > a['h']:
            novo={'id':f"FVG_{meio['t']}_B",'tipo':'FVG_bullish','direcao':'alta','top':atual['l'],'bottom':a['h'],
                  'created_ts':atual['t'],'origin_ts':meio['t'],'state':'ATIVA','flip_ts':None,'mother_fvg_id':None,
                  'first_touch_ts':None,'mitigated_ts':None,'invalidated_ts':None,
                  'source_a':dict(a),'source_mid':dict(meio),'source_c':dict(atual),'flip_candle':None}
        elif atual['h'] < a['l']:
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
                    z['state']='IFVG'; z['tipo']='IFVG_bearish'; z['direcao']='baixa'; z['mother_fvg_id']=z['id']; z['flip_ts']=atual['t']; z['flip_candle']=dict(atual)
                elif atual['l'] <= z['bottom']:
                    z['state']='PARCIAL'; z['first_touch_ts']=z['first_touch_ts'] or atual['t']
                    z['mitigated_ts']=z['mitigated_ts'] or atual['t']
                elif atual['l'] < z['top']:
                    z['state']='PARCIAL' if atual['l'] < (z['top']+z['bottom'])/2 else 'TOCADA'
                    z['first_touch_ts']=z['first_touch_ts'] or atual['t']
            elif z['tipo'] == 'FVG_bearish':
                if atual['c'] > z['top']:
                    z['state']='IFVG'; z['tipo']='IFVG_bullish'; z['direcao']='alta'; z['mother_fvg_id']=z['id']; z['flip_ts']=atual['t']; z['flip_candle']=dict(atual)
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


def auditar_fvg_ifvg_h1_btc(dias=7, fim_ts_ms=None, lo=85000.0, hi=87000.0):
    """Auditoria read-only: prova geometria 3-candles e lifecycle FVG/IFVG H1."""
    raw=_fetch_bybit_klines_historico('BTCUSDT','60',dias,fim_ts_ms)
    candles,_=_validar_e_limpar_candles(raw,'H1')
    states=_kairos_fvg_states(candles,lookback=max(250,len(candles)))
    out=[]
    for z in states:
        if z.get('top',0) < lo or z.get('bottom',0) > hi:
            continue
        a=z.get('source_a') or {}; m=z.get('source_mid') or {}; cc=z.get('source_c') or {}
        flip=z.get('flip_candle') or {}
        geometry_ok = (
            (z.get('direcao')=='alta' and cc.get('l') is not None and a.get('h') is not None and cc['l']>a['h'])
            or (z.get('direcao')=='baixa' and cc.get('h') is not None and a.get('l') is not None and cc['h']<a['l'])
            or z.get('tipo','').startswith('IFVG_')
        )
        mother_type = 'FVG_bullish' if z.get('id','').endswith('_B') else 'FVG_bearish'
        flip_ok=None
        if z.get('flip_ts') is not None:
            flip_ok = (flip.get('c') < z.get('bottom')) if mother_type=='FVG_bullish' else (flip.get('c') > z.get('top'))
        out.append({
            'id':z.get('id'),'mother_fvg_id':z.get('mother_fvg_id'),'mother_type':mother_type,'current_type':z.get('tipo'),
            'state':z.get('state'),'bottom':z.get('bottom'),'top':z.get('top'),
            'created_ts':z.get('created_ts'),'flip_ts':z.get('flip_ts'),
            'first_touch_ts':z.get('first_touch_ts'),'invalidated_ts':z.get('invalidated_ts'),
            'source_a':a,'source_mid':m,'source_c':cc,'flip_candle':flip or None,
            'geometry_3c_ok':geometry_ok,'flip_close_ok':flip_ok,
        })
    return {'pair':'BTCUSD','market_symbol':'BTCUSDT','tf':'H1','range':[lo,hi],'candles':len(candles),'zones':out}

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
    """Sweep contra POOLS confirmados (não contra swing isolado).
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

KAIROS_REAL_LIQUIDITY_AUDIT_TFS = {
    'MN': ('M', 3650),
    'W1': ('W', 1825),
    'D1': ('D', 730),
    'H4': ('240', 120),
    'H1': ('60', 45),
    'M30': ('30', 25),
    'M15': ('15', 12),
    'M5': ('5', 5),
    'M1': ('1', 2),
}


def _kairos_audit_lux50_structural_levels(candles, tf, now_ts, sample_limit=100):
    """Audita a genealogia causal dos Swing High/Low Lux50 sem alterar produção."""
    levels = _kairos_lux50_structural_levels(candles, tf, now_ts)
    by_ts = {c['t']: i for i, c in enumerate(candles)}
    checks = []
    for lv in levels[-sample_limit:]:
        oi = by_ts.get(lv.get('origin_ts'))
        expected_ci = None if oi is None else oi + KAIROS_STRUCTURAL_SWING_SIZE
        ci_ok = expected_ci is not None and expected_ci < len(candles)
        expected_confirm = candles[expected_ci]['t'] if ci_ok else None
        typ = lv.get('type')
        expected_level = None
        if oi is not None:
            expected_level = candles[oi]['h'] if typ == 'SWING_HIGH' else candles[oi]['l']
        first_capture = None
        if ci_ok and expected_level is not None:
            for cc in candles[expected_ci + 1:]:
                crossed = (typ == 'SWING_HIGH' and cc['h'] > expected_level) or (typ == 'SWING_LOW' and cc['l'] < expected_level)
                if crossed:
                    first_capture = cc['t']
                    break
        checks.append({
            'tf': tf, 'type': typ, 'level': lv.get('level'),
            'origin_ts': lv.get('origin_ts'), 'confirmed_ts': lv.get('confirmed_ts'),
            'captured_ts': lv.get('captured_ts'),
            'origin_price_ok': expected_level == lv.get('level'),
            'confirmation_delay_ok': ci_ok and expected_confirm == lv.get('confirmed_ts'),
            'known_before_capture': lv.get('captured_ts') is None or (lv.get('confirmed_ts') is not None and lv.get('confirmed_ts') < lv.get('captured_ts')),
            'first_capture_ok': first_capture == lv.get('captured_ts'),
            'lookahead_ok': lv.get('confirmed_ts') is not None and lv.get('confirmed_ts') <= now_ts,
        })
    for x in checks:
        x['pass'] = bool(x['origin_price_ok'] and x['confirmation_delay_ok'] and x['known_before_capture'] and x['first_capture_ok'] and x['lookahead_ok'])
    return {
        'tf': tf, 'candles': len(candles), 'swing_size': KAIROS_STRUCTURAL_SWING_SIZE,
        'total': len(checks), 'pass': sum(1 for x in checks if x['pass']),
        'fail': sum(1 for x in checks if not x['pass']),
        'swing_high': sum(1 for x in checks if x['type'] == 'SWING_HIGH'),
        'swing_low': sum(1 for x in checks if x['type'] == 'SWING_LOW'),
        'all_math_pass': all(x['pass'] for x in checks),
        'samples': checks[-20:],
    }


def auditar_liquidez_real_todos_tfs(pair='BTCUSD', sample_limit=100, fim_ts_ms=None):
    """Auditoria on-demand MN→M1 dos Swing High/Low Lux50, sem tocar no motor de sinais."""
    pair = str(pair or 'BTCUSD').upper()
    if pair not in PARES_MONITORADOS_REPLAY:
        raise ValueError('par fora da lista monitorada')
    sample_limit = max(10, min(int(sample_limit or 100), 300))
    now_ts = int(fim_ts_ms) if fim_ts_ms else int(time.time() * 1000)
    resultado = {}
    for tf, (interval, dias) in KAIROS_REAL_LIQUIDITY_AUDIT_TFS.items():
        candles = _fetch_bybit_klines_historico(pair, interval, dias, fim_ts_ms=fim_ts_ms)
        resultado[tf] = _kairos_audit_lux50_structural_levels(candles, tf, now_ts, sample_limit)
    total = sum(r['total'] for r in resultado.values())
    total_fail = sum(r['fail'] for r in resultado.values())
    return {
        'pair': pair, 'swing_math': 'LUX50', 'timeframes': list(KAIROS_REAL_LIQUIDITY_AUDIT_TFS.keys()),
        'total_levels_checked': total, 'total_fail': total_fail,
        'all_math_pass': total > 0 and total_fail == 0,
        'resultado': resultado,
        'nota': 'Prova origem→confirmacao→primeira captura→zero lookahead dos Swing High/Low Lux50. Somente auditoria; nao altera setup/entry/SL/TP.'
    }



def _kairos_audit_structure_sl_parity_tf(candles, tf, swing_size=5, sample_limit=100):
    """Audita BOS/CHoCH -> broken swing -> protected swing de forma causal.

    Importante: um pivot Lux só é "conhecido" quando a mudança de leg o
    CONFIRMA. O timestamp de origem sozinho não basta. Eventos sem swing
    oposto confirmado são inelegíveis para SL e não contam como erro de
    paridade — o motor deve simplesmente não abrir trade neles.
    """
    events=compute_lux_structure_events(candles, swing_size=swing_size)
    swings=_extrair_swings_lux_algo(candles, swing_size=swing_size)
    by_ts={x.get('t'):x for x in candles}
    idx_by_ts={x.get('t'):i for i,x in enumerate(candles)}

    # A própria matemática Lux confirma o pivot swing_size candles depois
    # da origem (na mudança de leg). Guardamos isso explicitamente para a
    # auditoria nunca usar um pivot futuro só porque a origem já existia.
    confirmed_swings=[]
    for s in swings:
        oi=idx_by_ts.get(s.get('t'))
        ci=(oi+swing_size) if oi is not None else None
        confirm_ts=candles[ci]['t'] if ci is not None and ci < len(candles) else None
        confirmed_swings.append({**s,'confirm_ts':confirm_ts})
    swing_keys={(s.get('t'),str(s.get('tipo')).upper(),float(s.get('valor'))) for s in confirmed_swings}

    rows=[]; fails=0; eligible=0; skipped_no_anchor=0
    for e in events[-max(1,int(sample_limit)):]:
        direction='LONG' if e.get('direcao')=='alta' else 'SHORT'
        expected_protected='LOW' if direction=='LONG' else 'HIGH'
        broken_type='HIGH' if direction=='LONG' else 'LOW'
        broken_ts=e.get('broken_swing_origin_ts')
        protected_ts=e.get('protected_swing_origin_ts')
        broken=float(e.get('nivel')) if e.get('nivel') is not None else None
        protected=float(e.get('protected_swing_level')) if e.get('protected_swing_level') is not None else None
        break_ts=e.get('t')
        break_candle=by_ts.get(break_ts)
        broken_candle=by_ts.get(broken_ts)
        protected_candle=by_ts.get(protected_ts)

        # Só swings já CONFIRMADOS até o candle da quebra podem participar.
        known=[s for s in confirmed_swings if s.get('confirm_ts') is not None and break_ts is not None and s['confirm_ts']<=break_ts]
        known_opposite=[s for s in known if str(s.get('tipo')).upper()==expected_protected]
        latest=max(known_opposite,key=lambda s:(s['confirm_ts'],s['t'])) if known_opposite else None

        broken_price_ok=bool(broken_candle and broken is not None and abs(float(broken_candle['h' if broken_type=='HIGH' else 'l'])-broken)<=1e-9)
        broken_is_lux=bool(broken is not None and (broken_ts,broken_type,broken) in swing_keys)
        close_break_ok=bool(break_candle and broken is not None and (break_candle['c']>broken if direction=='LONG' else break_candle['c']<broken))
        type_ok=e.get('protected_swing_type')==expected_protected

        # Sem swing oposto causalmente confirmado = sem âncora de SL = sem trade.
        # Isso é comportamento correto, não falha matemática.
        no_anchor = latest is None
        if no_anchor:
            skipped_no_anchor += 1
            rows.append({
                'pass':True,'eligible_for_sl':False,'reason':'SEM_SWING_OPOSTO_CONFIRMADO',
                'tf':tf,'event_type':e.get('tipo'),'direction':direction,
                'break_ts':break_ts,'broken_level':broken,'broken_origin_ts':broken_ts
            })
            continue

        eligible += 1
        protected_price_ok=bool(protected_candle and protected is not None and abs(float(protected_candle['l' if expected_protected=='LOW' else 'h'])-protected)<=1e-9)
        protected_is_lux=bool(protected is not None and (protected_ts,expected_protected,protected) in swing_keys)
        latest_ok=bool(protected_ts==latest.get('t') and protected is not None and abs(protected-float(latest.get('valor')))<=1e-9)
        protected_confirm_ts=latest.get('confirm_ts') if latest_ok else None
        causal_ok=bool(
            broken_ts is not None and protected_ts is not None and break_ts is not None
            and broken_ts<break_ts and protected_ts<break_ts
            and protected_confirm_ts is not None and protected_confirm_ts<=break_ts
        )

        ok=all((broken_price_ok,protected_price_ok,broken_is_lux,protected_is_lux,close_break_ok,causal_ok,type_ok,latest_ok))
        if not ok: fails+=1
        rows.append({
            'pass':ok,'eligible_for_sl':True,'tf':tf,'event_type':e.get('tipo'),'direction':direction,
            'break_ts':break_ts,'break_close':break_candle.get('c') if break_candle else None,
            'broken_type':broken_type,'broken_level':broken,'broken_origin_ts':broken_ts,
            'protected_type':e.get('protected_swing_type'),'protected_level':protected,
            'protected_origin_ts':protected_ts,'protected_confirm_ts':protected_confirm_ts,
            'checks':{'broken_price_ok':broken_price_ok,'protected_price_ok':protected_price_ok,
                      'broken_is_lux':broken_is_lux,'protected_is_lux':protected_is_lux,
                      'close_break_ok':close_break_ok,'causal_ok':causal_ok,'type_ok':type_ok,
                      'latest_opposite_swing_ok':latest_ok}
        })
    return {'tf':tf,'swing_size':swing_size,'events_checked':len(rows),
            'eligible_for_sl':eligible,'skipped_no_anchor':skipped_no_anchor,
            'fail':fails,'all_pass':eligible>0 and fails==0,'events':rows}

def auditar_structure_sl_btc(sample_limit=100, fim_ts_ms=None):
    """Prova auditavel do elo estrutura Lux -> protected swing usado pelo SL em M15/M5."""
    result={}
    specs={'M15':('15',8),'M5':('5',4)}
    for tf,(interval,dias) in specs.items():
        candles=_fetch_bybit_klines_historico('BTCUSD',interval,dias,fim_ts_ms=fim_ts_ms)
        result[tf]=_kairos_audit_structure_sl_parity_tf(candles,tf,swing_size=5,sample_limit=sample_limit)
    total=sum(x['events_checked'] for x in result.values())
    fail=sum(x['fail'] for x in result.values())
    out={'pair':'BTCUSD','audit':'LUX_BREAK_PROTECTED_SWING_SL_PARITY','total_events_checked':total,
         'total_fail':fail,'all_pass':total>0 and fail==0,'resultado':result,
         'nota':'Auditoria somente leitura. Confere origem do swing quebrado, close da quebra, swing protegido oposto, causalidade e se era o ultimo swing Lux oposto conhecido no instante do BOS/CHoCH.'}
    print('[STRUCTURE_SL_PARITY] '+json.dumps({'pair':'BTCUSD','total':total,'fail':fail,'all_pass':out['all_pass']}),flush=True)
    return out


@explicacao_bp.route('/kairos_v2/auditoria_structure_sl_btc', methods=['GET'])
def auditoria_structure_sl_btc_endpoint():
    try:
        n=max(1,min(int(request.args.get('eventos','100')),300))
        fim=request.args.get('fim_ts_ms')
        fim=int(fim) if fim else None
        return jsonify(auditar_structure_sl_btc(sample_limit=n,fim_ts_ms=fim)),200
    except Exception as e:
        import traceback
        traceback.print_exc()
        return jsonify({'erro':str(e)}),500


@explicacao_bp.route('/kairos_v2/auditoria_liquidez_real', methods=['GET'])
def auditoria_liquidez_real_endpoint():
    try:
        pair = request.args.get('pair', 'BTCUSD')
        n = int(request.args.get('eventos', 100))
        fim = request.args.get('fim_ts_ms')
        fim = int(fim) if fim else None
        return jsonify(auditar_liquidez_real_todos_tfs(pair=pair, sample_limit=n, fim_ts_ms=fim)), 200
    except Exception as e:
        import traceback
        traceback.print_exc()
        return jsonify({'erro': str(e)}), 500


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


@explicacao_bp.route('/kairos_v2/auditoria_btc_liquidez', methods=['GET'])
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

        # FIRST CAPTURE precisa ser demonstrável no TF nativo ANTES de refinarmos
        # o instante no M15. Isso impede uma captura antiga/consumida de reaparecer
        # como "nova" só porque a janela M15 curta não contém o primeiro sweep.
        native_capture_ts=liq.get('captured_ts')
        if liq.get('state') != 'CAPTURED' or native_capture_ts is None:
            continue
        tf_ms={'W1':7*86400000,'D1':86400000,'H4':4*3600000,'H1':3600000}.get(liq.get('tf'))
        if tf_ms is None:
            continue
        native_capture_end=native_capture_ts + tf_ms
        # Se o candle nativo da PRIMEIRA captura ficou fora do histórico M15,
        # não inventamos timing: o nível é consumido e inelegível para setup.
        if not m15 or native_capture_end <= m15[0]['t']:
            continue

        first_idx=None
        for i,c in enumerate(m15):
            if c['t'] <= confirm_ts:
                continue
            if c['t'] < native_capture_ts or c['t'] >= native_capture_end:
                continue
            if (is_high and c['h'] > level) or ((not is_high) and c['l'] < level):
                first_idx=i; break
        if first_idx is None:
            continue
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
    # Um capture HTF não congela o intraday inteiro. Depois que uma captura
    # posterior H1/H4/D1/W1 já está causalmente confirmada, ela representa um
    # novo evento de liquidez e pode abrir uma nova tese dentro do mesmo range.
    # A hierarquia HTF continua sendo contexto; para a TESE OPERACIONAL escolhemos
    # o evento válido mais recente. O peso do TF desempata capturas simultâneas.
    valid.sort(key=lambda x:(x['sweep_ts'],KAIROS_PRIMARY_LIQUIDITY_PRIORITY.get(x['liquidity_tf'],0)), reverse=True)
    selected=valid[0]
    return selected, {'levels':levels,'setup_levels':setup_levels,'candidates':candidates,
                      'valid_capture_count':len(valid),
                      'selection_rule':'LATEST_VALID_CAPTURE_THEN_HTF_PRIORITY',
                      'selected_capture':{k:selected.get(k) for k in ('liquidity_tf','liquidity_type','nivel','sweep_ts','confirm_ts','post_capture_state')}}


def _kairos_direction_after_first_capture(candles, capture, swing_size=5):
    """Deriva direção da REAÇÃO + intenção + MSS/CHoCH, nunca do lado da liquidez."""
    if not candles or not capture: return None
    side=capture.get('liquidity_side'); state=capture.get('post_capture_state')
    # Sem reação resolvida não existe direção. Nunca transformar estado desconhecido
    # silenciosamente em continuação.
    if side not in ('HIGH','LOW') or state not in ('REJECTION_RECLAIM','ACCEPTANCE_CONTINUATION'):
        return None
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

def _kairos_m5_refine_zone(m5_candles, m15_zone, sweep_ts, structure_ts, direction):
    """Refina M15 com POI M5 da MESMA perna causal sweep→MSS.

    O FVG pode nascer durante o displacement, antes do candle que confirma
    MSS/CHoCH: SWEEP_TS <= FVG_CREATED_TS <= STRUCTURE_TS. A entrada/reteste,
    porém, continua proibida antes da confirmação estrutural.
    """
    if not m5_candles or not m15_zone:
        return None
    zones=[]
    for z in _kairos_fvg_states(m5_candles):
        created=z.get('created_ts')
        eff=z.get('flip_ts') or created or 0
        if created is None or created < sweep_ts or created > structure_ts:
            continue
        if eff > structure_ts or z.get('direcao') != direction:
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
            # O obstáculo precisa existir causalmente no momento da entrada.
            created_ts = z.get('flip_ts') or z.get('break_ts') or z.get('created_ts') or z.get('t')
            if created_ts is not None and z.get('tipo','').startswith('FVG_'):
                created_ts = z.get('created_ts')
            obs.append({'tf':tf,'tipo':z.get('tipo','POI_OPPOSTA'),'nivel':level,'top':top,'bottom':bottom,
                        'peso':KAIROS_TF_PESO.get(tf,1),'dist':abs(level-entry),'classe':'OBSTACULO_POI',
                        'state':z.get('state'),'created_ts':created_ts,
                        'first_touch_ts':z.get('first_touch_ts'),'mitigated_ts':z.get('mitigated_ts'),
                        'invalidated_ts':z.get('invalidated_ts')})
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
    """SL 1:1 com a mesma matemática Lux/internal que gerou o BOS/CHoCH.

    O próprio evento estrutural carrega o swing protegido que existia no
    instante da quebra. Não reconstruímos pivôs depois, não usamos HTF como
    fallback e não criamos uma segunda matemática para o stop.
    """
    if not exec_candles or not structure or not retest:
        return None, {'motivo':'DADOS_SL_INSUFICIENTES','candidatos':[]}

    entry=float(retest['c'])
    retest_ts=retest['t']
    expected_type='LOW' if direction=='LONG' else 'HIGH'
    base=structure.get('protected_swing_level')
    base_ts=structure.get('protected_swing_origin_ts')
    actual_type=structure.get('protected_swing_type')

    audit=[{
        'tf':exec_tf,
        'classe':'LUX_PROTECTED_SWING_FROM_BREAK',
        'structure_ts':structure.get('t'),
        'structure_type':structure.get('tipo'),
        'broken_level':structure.get('nivel'),
        'protected_type':actual_type,
        'anchor_ts':base_ts,
        'sweep_level':base,
        'sweep_extreme':base,
    }]

    if base is None or base_ts is None or actual_type != expected_type:
        audit[0]['status']='SEM_PROTECTED_SWING_NO_EVENTO'
        return None, {'motivo':'SEM_SWING_PROTEGIDO_EXEC_TF','candidatos':audit}

    base=float(base)
    if (direction=='LONG' and base>=entry) or (direction=='SHORT' and base<=entry):
        audit[0]['status']='LADO_ERRADO'
        return None, {'motivo':'SWING_PROTEGIDO_LADO_ERRADO','candidatos':audit}

    known=[x for x in exec_candles if x['t']<=retest_ts]
    sl=aplicar_buffer_stop_atr(base,'alta' if direction=='LONG' else 'baixa',known)
    audit[0]['sl_buffered']=sl
    if sl is None:
        audit[0]['status']='SEM_SL'
        return None, {'motivo':'SEM_SL_BUFFER','candidatos':audit}

    # Se o swing protegido já foi violado antes da entrada, a tese local morreu.
    posteriores=[x for x in exec_candles if base_ts < x['t'] < retest_ts]
    violation=next((x for x in posteriores if (x['l']<=sl if direction=='LONG' else x['h']>=sl)),None)
    if violation:
        audit[0]['status']='INVALIDADA_ANTES_ENTRY'
        audit[0]['invalidated_ts']=violation['t']
        return None, {'motivo':'SWING_PROTEGIDO_INVALIDADO_ANTES_ENTRY','candidatos':audit}

    audit[0]['status']='VALIDA'
    return {
        'sl':sl,'sl_base':base,'sl_tf':exec_tf,
        'sl_classe':'LUX_PROTECTED_SWING_FROM_BREAK',
        'sl_sweep_ts':base_ts,'sl_sweep_level':base,'sl_sweep_extreme':base
    }, {'motivo':'OK_LUX_SAME_EVENT','candidatos':audit}

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
        # O POI de entrada tem de NASCER da perna estrutural e existir até o break.
        # A geometria pode começar no candle do sweep, mas nunca antes dele nem depois do MSS/BOS.
        if not (sweep['sweep_ts'] <= effective_ts <= st):
            continue
        if z.get('created_ts') is not None and z.get('created_ts') < sweep['sweep_ts']:
            # IFVG também precisa de FVG-mãe criada dentro da perna causal; não aceitamos
            # inverter uma FVG antiga e chamá-la de POI produzido pelo displacement atual.
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
    """Primeiro reteste causal em candle POSTERIOR à estrutura/POI.

    O candle que confirma MSS/BOS, cria a FVG ou confirma o flip IFVG nunca
    pode ser simultaneamente o candle de reteste/entrada.
    """
    if not zone:
        return None
    zone_ready_ts=zone.get('flip_ts') or zone.get('created_ts') or zone.get('break_ts') or zone.get('t') or 0
    ready_ts=max(after_ts or 0, zone_ready_ts)
    for c in candles:
        if c['t'] <= ready_ts:
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



def _kairos_experimental_zone_id(z):
    if not z:
        return None
    return (z.get('tipo'), z.get('created_ts'), z.get('flip_ts'), z.get('bottom'), z.get('top'))

def _kairos_experimental_eligible_entry_zones(exec_candles, sweep, structure, mapa):
    """Experimental mirror of current eligibility rules; branch-only."""
    if not structure:
        return []
    direction=sweep['direcao']; st=structure['t']; zones=[]
    for z in _kairos_fvg_states(exec_candles):
        effective_ts=z.get('flip_ts') or z.get('created_ts') or 0
        if z.get('direcao') != direction: continue
        if z.get('state') not in ('ATIVA','TOCADA','PARCIAL','IFVG'): continue
        if not (sweep['sweep_ts'] <= effective_ts <= st): continue
        if z.get('created_ts') is not None and z.get('created_ts') < sweep['sweep_ts']: continue
        z2=dict(z); z2['liquidity_inside']=_kairos_zone_contains_liquidity(z2,mapa); zones.append(z2)
    ob=_kairos_ob_from_break(exec_candles, structure.get('full_idx'), direction)
    if ob:
        ob['liquidity_inside']=_kairos_zone_contains_liquidity(ob,mapa); zones.append(ob)
    return zones

def _kairos_experimental_apply_poi_policy(current, exec_candles, sweep, structure, mapa, policy, state, audit=None):
    """Branch-only A/B/C lifecycle. Default evaluator never calls this unless policy is explicit."""
    thesis=(sweep.get('sweep_ts'), structure.get('t'), sweep.get('direcao'))
    current_id=_kairos_experimental_zone_id(current)
    if not policy or policy == 'A_CURRENT':
        if audit is not None:
            audit.update({'thesis_id':thesis,'event':'CURRENT','current_id':current_id,'selected_id':current_id,'previous_id':None})
        return current
    if state is None:
        if audit is not None:
            audit.update({'thesis_id':thesis,'event':'NO_STATE','current_id':current_id,'selected_id':current_id,'previous_id':None})
        return current
    eligible=_kairos_experimental_eligible_entry_zones(exec_candles,sweep,structure,mapa)
    by_id={_kairos_experimental_zone_id(z):z for z in eligible}
    prev=state.get(thesis); prev_id=_kairos_experimental_zone_id(prev)
    if prev is None:
        if current: state[thesis]=dict(current)
        if audit is not None:
            audit.update({'thesis_id':thesis,'event':'INIT','current_id':current_id,'selected_id':current_id,'previous_id':None,'eligible_count':len(eligible)})
        return current
    if prev_id in by_id:
        selected=by_id[prev_id]
        if audit is not None:
            audit.update({'thesis_id':thesis,'event':'KEEP','current_id':current_id,'selected_id':prev_id,'previous_id':prev_id,'eligible_count':len(eligible)})
        return selected
    if policy == 'B_FREEZE':
        if audit is not None:
            audit.update({'thesis_id':thesis,'event':'EXPIRED_FREEZE','current_id':current_id,'selected_id':None,'previous_id':prev_id,'eligible_count':len(eligible)})
        return None
    if policy == 'C_LIFECYCLE':
        if current: state[thesis]=dict(current)
        if audit is not None:
            audit.update({'thesis_id':thesis,'event':'REPLACE' if current else 'EXPIRED_NO_REPLACEMENT','current_id':current_id,'selected_id':current_id,'previous_id':prev_id,'eligible_count':len(eligible)})
        return current
    raise ValueError('experimental_poi_policy invalida: '+str(policy))


def avaliar_vortex_decision_layer_v2(m15_ate_agora, m5_ate_agora, d1_ate_agora=None,
                                      candles_por_tf=None, audit_pair=None,
                                      experimental_poi_policy=None, experimental_poi_state=None):
    """KAIROS Paper V2.2 — liquidez HTF estrutural, M15 executa, M5 refina.

    Cadeia autorizadora:
    W1/D1/H4/H1 structural liquidity -> neutral FIRST capture -> rejection/reclaim OR acceptance/continuation -> intention/displacement -> M15 MSS/CHoCH/BOS
    -> causal M15 FVG/IFVG/OB -> optional M5 refinement -> retest
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

    # A liquidez HTF autoriza a procura do gatilho. M15 continua preferencial;
    # para SCALP, se M15 ainda não confirmou, M5 pode confirmar a MESMA narrativa
    # causal depois do first capture. M5 nunca cria tese sozinho.
    exec_tf='M15'; exec_candles=candles_por_tf.get('M15') or []
    intent=_kairos_direction_after_first_capture(exec_candles,sweep,swing_size=5)
    resultado['intent_m15_found']=bool(intent)
    if not intent:
        m5_intent_candles=candles_por_tf.get('M5') or []
        intent=_kairos_direction_after_first_capture(m5_intent_candles,sweep,swing_size=5)
        if intent:
            exec_tf='M5'; exec_candles=m5_intent_candles
            resultado['intent_fallback']='M5_AFTER_HTF_FIRST_CAPTURE'
    if not intent:
        resultado['failure_reason']='SEM_INTENCAO_CHOCH_MSS_M15_M5_APOS_FIRST_CAPTURE'; return resultado
    direction=intent['direction']; sweep['direcao']=intent['direcao']; structure=intent['structure']
    resultado['direction']=direction; resultado['execution_tf']=exec_tf; resultado['choch_confirmed']=True
    resultado['choch_timestamp']=structure['t']; resultado['choch_level']=round(structure['nivel'],6)
    z=intent.get('momentum_z'); resultado['momentum_z']=round(z,3) if z is not None else None

    ctx=contexto.get('final'); trade_dir=intent['direcao']
    if ctx==trade_dir: resultado['setup_type']='TREND'
    elif intent['mode']=='CONTINUATION': resultado['setup_type']='INTERNAL_CONTINUATION'
    elif ctx in ('alta','baixa'): resultado['setup_type']='PULLBACK_REVERSAL'
    else: resultado['setup_type']='LOCAL'

    zone=_kairos_select_entry_zone(exec_candles,sweep,structure,mapa)
    if experimental_poi_policy:
        lifecycle_audit={}
        zone=_kairos_experimental_apply_poi_policy(zone,exec_candles,sweep,structure,mapa,experimental_poi_policy,experimental_poi_state,lifecycle_audit)
        resultado['experimental_poi_lifecycle']=lifecycle_audit
    if not zone:
        resultado['failure_reason']=f'SEM_FVG_IFVG_OB_{exec_tf}_CAUSAL'; return resultado
    # SHADOW ONLY: prova matemática/causal do POI escolhido. Não bloqueia nem altera sinal.
    try:
        poi_shadow=_kairos_shadow_validate_poi(zone, exec_candles, sweep=sweep, structure=structure, tf=exec_tf)
        resultado['poi_shadow_audit']=poi_shadow
        _kairos_shadow_log_poi(audit_pair, zone, poi_shadow)
    except Exception as _poi_shadow_exc:
        resultado['poi_shadow_audit']={'shadow_only':True,'pass':False,'reason':f'AUDIT_EXCEPTION:{_poi_shadow_exc}'}
    resultado['zone_type']=zone['tipo']; resultado['zone_top']=round(zone['top'],6); resultado['zone_bottom']=round(zone['bottom'],6)
    resultado['zone_source']=f"{zone['tipo']}_{exec_tf}_APOS_SWEEP"; resultado['liquidity_inside_zone']=zone.get('liquidity_inside',[])
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

    # Se a confirmação veio no M15, M5 pode refinar. Se a confirmação já veio
    # no M5, a própria zona M5 é a zona executável e não há segundo refinamento.
    m5=candles_por_tf.get('M5') or []
    refined=_kairos_m5_refine_zone(m5,zone,sweep['sweep_ts'],structure['t'],sweep['direcao']) if exec_tf=='M15' else None
    retest=None; active_zone=zone; entry_tf=exec_tf
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
                    pre_ts=structure['t']
                    pre_tf_map={tf:[c for c in cs if c.get('t') is not None and c['t'] <= pre_ts] for tf,cs in candles_por_tf.items()}
                    pre_targets = _kairos_structural_targets(
                        pre_tf_map, pre_ts, pre_limit, direction, limit=12
                    )
                    if pre_targets:
                        pre_target = pre_targets[0]
                        pre_obstacles = _kairos_opposing_zone_obstacles(
                            _kairos_build_mtf_map(pre_tf_map), pre_limit, direction,
                            target_level=pre_target['nivel'], limit=8,
                            allowed_tfs=('M15','H1','H4','D1','W1')
                        )
                        # Pré-alerta usa a mesma gestão operacional do sinal:
                        # 2R parcial + BE, 3R final. Liquidez/POIs continuam como
                        # validação de espaço; não viram alvo remoto arbitrário.
                        pre_sign = 1.0 if direction == 'LONG' else -1.0
                        pre_blocked = False
                        if pre_obstacles:
                            pre_o = pre_obstacles[0]
                            pre_o_rr = abs(float(pre_o['nivel']) - pre_limit) / pre_risk
                            if pre_o_rr < 2.0:
                                pre_blocked = True
                        pre_struct_rr = abs(float(pre_target['nivel']) - pre_limit) / pre_risk
                        if pre_struct_rr < 2.0:
                            pre_blocked = True
                        if not pre_blocked:
                            resultado['prealert_tp1'] = round(pre_limit + pre_sign * 2.0 * pre_risk, 6)
                            resultado['prealert_tp1_rr'] = 2.0
                            resultado['prealert_tp1_origem'] = 'GESTAO_FIXA_2R_PARCIAL_BE'
                            resultado['prealert_tp2'] = round(pre_limit + pre_sign * 3.0 * pre_risk, 6)
                            resultado['prealert_tp2_rr'] = 3.0
                            resultado['prealert_tp2_origem'] = 'GESTAO_FIXA_3R'
                            resultado['prealert_tp'] = resultado['prealert_tp2']
                            resultado['prealert_rr'] = 3.0
                            resultado['prealert_tp_origem'] = resultado['prealert_tp2_origem']

        resultado['failure_reason']='AGUARDANDO_RETESTE_ZONA'; return resultado
    entry=retest['c']; resultado['entry']=round(entry,6); resultado['timestamp']=retest['t']

    # O HTF autoriza a narrativa; o risco pertence SEMPRE ao menor TF que
    # confirmou a quebra estrutural (M15 preferencial, M5 quando foi o fallback).
    # Reversal e continuation obedecem à mesma regra de invalidação local.
    sl_info,sl_audit=_kairos_select_structural_sl(mapa,exec_tf,exec_candles,sweep,structure,retest,direction)
    resultado['sl_audit']=sl_audit
    if not sl_info:
        resultado['failure_reason']='SEM_ANCORA_SL_CAUSAL_VALIDA'; return resultado
    sl=sl_info['sl']; risk=abs(entry-sl)
    if risk<=0:
        resultado['failure_reason']='RISCO_ZERO_SL'; return resultado
    resultado['sl']=round(sl,6)
    resultado['sl_regra']=f"{sl_info.get('sl_classe','STRUCTURAL')}_atr_buffer"
    resultado['sl_anchor_tf']=sl_info.get('sl_tf')
    resultado['sl_anchor_class']=sl_info.get('sl_classe')
    resultado['sl_anchor_sweep_ts']=sl_info.get('sl_sweep_ts')
    resultado['sl_anchor_extreme']=round(sl_info['sl_sweep_extreme'],6) if sl_info.get('sl_sweep_extreme') is not None else None

    # TP = primeira liquidez estrutural ATIVA do lado do trade. POI contrário antes dela pode virar TP conservador.
    # Congela mapa/targets no timestamp da entrada: nenhum candle posterior ao
    # reteste pode criar, consumir ou mover a liquidez usada como TP.
    entry_ts=retest['t']
    target_tf_map={tf:[c for c in cs if c.get('t') is not None and c['t'] <= entry_ts] for tf,cs in candles_por_tf.items()}
    targets=_kairos_structural_targets(target_tf_map,entry_ts,entry,direction,limit=12)
    for t in targets: t['rr']=round(t['dist']/risk,2) if risk else None
    resultado['next_liquidity_targets']=targets[:8]
    if not targets:
        resultado['failure_reason']='SEM_LIQUIDEZ_ESTRUTURAL_ALVO'; return resultado
    target=targets[0]; resultado['first_liquidity_target']=dict(target); resultado['tp_final_liquidez']=round(target['nivel'],6)

    obstacle_tfs=('M15','H1','H4','D1','W1')
    obstacles=_kairos_opposing_zone_obstacles(_kairos_build_mtf_map(target_tf_map),entry,direction,target_level=target['nivel'],limit=8,allowed_tfs=obstacle_tfs)
    for o in obstacles: o['rr']=round(o['dist']/risk,2) if risk else None
    resultado['target_obstacles']=obstacles
    # Gestão operacional fixa e auditável:
    # TP1 = 2R (parcial + mover SL para BE); TP2 = 3R.
    # A liquidez estrutural continua mapeada como contexto/obstáculo, mas não
    # transforma um swing remoto em TP de 10R/30R. Se houver obstáculo estrutural
    # relevante ANTES de 2R, rejeitamos o setup em vez de fabricar RR.
    sign = 1.0 if direction == 'LONG' else -1.0
    tp1_2r = entry + sign * (2.0 * risk)
    tp2_3r = entry + sign * (3.0 * risk)

    # Estado do obstáculo no instante da entrada: o mapa já foi truncado em
    # entry_ts. Não basta a zona existir; distinguimos fresh vs previamente
    # tocada/mitigada para auditoria e exigimos que ela ainda esteja válida.
    active_obstacles=[]
    for o in obstacles:
        created=o.get('created_ts')
        invalidated=o.get('invalidated_ts')
        if created is not None and created > entry_ts:
            continue
        if invalidated is not None and invalidated <= entry_ts:
            continue
        touch=o.get('first_touch_ts')
        raw_state=o.get('state')
        o['entry_state']='FRESH_ACTIVE' if touch is None or touch >= entry_ts else 'MITIGATED_ACTIVE'
        # Gate de risco: zona já tocada/parcialmente mitigada não recebe o mesmo
        # poder de veto de uma POI fresh. Ela continua no mapa/telemetria, mas só
        # FRESH/ATIVA ou IFVG efetivamente flipada podem bloquear o trade.
        # Um POI contrário só é hard-block se estiver À FRENTE da entrada.
        # Se a entrada já abriu além da borda proximal da zona, o preço já está
        # negociando através/para dentro do POI; nesse caso ele vira contexto/TP
        # conservador, não veto automático. Isso evita o falso bloqueio observado
        # no BTC SHORT: entry 85379.3 acima da FVG D1 bullish cujo top era 85066.3.
        proximal = float(o.get('bottom')) if direction=='LONG' else float(o.get('top'))
        ahead_of_entry = (proximal > entry) if direction=='LONG' else (proximal < entry)
        o['ahead_of_entry'] = ahead_of_entry
        # Hierarquia MTF: W1/D1/H4 definem contexto/narrativa; não são
        # paredes binárias para um scalp interno M15/M5. H1/M15 podem vetar a
        # execução quando uma POI oposta fresh/IFVG está realmente à frente.
        # POIs HTF continuam preservadas em target_obstacles_at_entry para
        # contexto, reação, alvo e auditoria.
        execution_block_tf = o.get('tf') in ('H1','M15')
        o['role_at_entry'] = 'EXECUTION_OBSTACLE' if execution_block_tf else 'HTF_CONTEXT'
        # POI/FVG contrario nao decide direcao sozinho. Neste ponto a cadeia
        # causal ja provou sweep -> reaction/intention -> MSS/CHoCH -> displacement
        # -> POI causal -> reteste. Portanto uma zona H1/M15 contraria e fresh
        # continua como RISCO/CONTEXTO, mas nao veta cegamente uma intencao ja
        # confirmada. Hard-block fica reservado a IFVG contrario confirmado
        # (flip estrutural real), que representa mudanca de estado da propria zona.
        o['intention_direction_at_entry'] = direction
        o['causal_intention_confirmed'] = True

        # IFVG nao e parede automatica. No timestamp da entrada, classifica a
        # relacao REAL do preco com a zona usando apenas candles ja fechados.
        # - ACCEPTED_THROUGH: fechou para alem da borda distal na direcao do trade.
        # - REJECTED_AGAINST: tocou a zona e fechou de volta contra o trade.
        # - UNRESOLVED: ainda nao ha prova suficiente; preserva o bloqueio.
        # Isto mantem a IFVG como risco estrutural sem deixar a polaridade decidir
        # a direcao depois de sweep -> intencao -> MSS/CHoCH ja confirmados.
        interaction_state = 'NOT_APPLICABLE'
        interaction_ts = None
        if raw_state == 'IFVG' and execution_block_tf and ahead_of_entry:
            zone_bottom=float(o.get('bottom')); zone_top=float(o.get('top'))
            # Julga a zona no timeframe DELA, nunca no TF de execucao.
            # Ex.: IFVG H1 => somente candles H1 fechados ate a entrada.
            obstacle_tf=o.get('tf')
            obstacle_candles=(target_tf_map.get(obstacle_tf) or [])
            known_obstacle=[cc for cc in obstacle_candles if cc.get('t') is not None and cc['t'] <= entry_ts]
            interaction_state='UNRESOLVED'
            for cc in known_obstacle:
                if direction=='LONG':
                    touched=float(cc['h']) >= zone_bottom
                    accepted=float(cc['c']) > zone_top
                    rejected=touched and float(cc['c']) < zone_bottom
                else:
                    touched=float(cc['l']) <= zone_top
                    accepted=float(cc['c']) < zone_bottom
                    rejected=touched and float(cc['c']) > zone_top
                if accepted:
                    interaction_state='ACCEPTED_THROUGH'; interaction_ts=cc['t']
                elif rejected:
                    interaction_state='REJECTED_AGAINST'; interaction_ts=cc['t']
        o['interaction_state_at_entry']=interaction_state
        o['interaction_state_ts']=interaction_ts
        o['blocks_entry'] = bool(
            execution_block_tf
            and ahead_of_entry
            and raw_state=='IFVG'
            and interaction_state != 'ACCEPTED_THROUGH'
        )
        active_obstacles.append(o)
    resultado['target_obstacles_at_entry']=active_obstacles
    blocking_obstacles=[o for o in active_obstacles if o.get('blocks_entry')]
    resultado['blocking_obstacles_at_entry']=blocking_obstacles
    first_obstacle = blocking_obstacles[0] if blocking_obstacles else None
    if first_obstacle:
        obstacle_level = float(first_obstacle['nivel'])
        obstacle_rr = abs(obstacle_level - entry) / risk
        resultado['tp1_obstacle'] = dict(first_obstacle)
        if obstacle_rr < 2.0:
            # Plano principal 2R/3R bloqueado. Não transformamos CHoCH/MSS em
            # entrada automática: a cadeia causal completa já foi validada acima.
            # SCALP 1R só existe quando há espaço estrutural real >=1R.
            resultado['swing_blocked_by_obstacle']=True
            resultado['swing_obstacle_rr']=round(obstacle_rr,2)
            if obstacle_rr >= 1.0:
                tp_scalp_1r = entry + sign * risk
                resultado['tp1']=round(tp_scalp_1r,6)
                resultado['tp1_rr']=1.0
                resultado['tp1_origem']='SCALP_CAUSAL_1R'
                resultado['tp2']=None
                resultado['tp2_rr']=None
                resultado['tp2_origem']=None
                resultado['tp']=resultado['tp1']
                resultado['rr']=1.0
                resultado['tp_origem']='SCALP_CAUSAL_1R'
                resultado['be_trigger']=None
                resultado['be_price']=None
                resultado['trade_mode']='SCALP_CAUSAL_1R'
                resultado['structural_target_context']=round(float(target['nivel']),6)
                resultado['structural_target_context_origin']=f"LIQUIDEZ_ESTRUTURAL_{target['tf']}_{target['tipo']}"
                resultado['signal']=True
                resultado['valid']=True
                resultado['reason']=(
                    f"LIQ={sweep['liquidity_tf']}:{sweep['liquidity_type']}@{sweep['nivel']} -> FIRST_CAPTURE_M15@{sweep['sweep_ts']} -> "
                    f"{sweep.get('post_capture_state')} -> INTENT={intent['mode']}:{direction} -> {structure['tipo']}_M15 -> DISPLACEMENT(z={resultado['momentum_z']}) -> "
                    f"{resultado['zone_source']} -> RETEST_{entry_tf} -> SL={sl_info.get('sl_classe')} -> SCALP_CAUSAL_1R -> "
                    f"SETUP={resultado['setup_type']}"
                )
                return resultado
            resultado['failure_reason']='OBSTACULO_ESTRUTURAL_ANTES_1R'
            resultado['rr']=round(obstacle_rr,2)
            return resultado

    # A primeira liquidez estrutural do lado do trade também precisa deixar
    # espaço mínimo para o plano 2R. Ela continua registrada como alvo/contexto.
    structural_rr = abs(float(target['nivel']) - entry) / risk
    if structural_rr < 2.0:
        resultado['rr']=round(structural_rr,2)
        if structural_rr >= 1.0:
            tp_scalp_1r = entry + sign * risk
            resultado['tp1']=round(tp_scalp_1r,6)
            resultado['tp1_rr']=1.0
            resultado['tp1_origem']='SCALP_CAUSAL_1R'
            resultado['tp2']=None
            resultado['tp2_rr']=None
            resultado['tp2_origem']=None
            resultado['tp']=resultado['tp1']
            resultado['rr']=1.0
            resultado['tp_origem']='SCALP_CAUSAL_1R'
            resultado['be_trigger']=None
            resultado['be_price']=None
            resultado['trade_mode']='SCALP_CAUSAL_1R'
            resultado['signal']=True
            resultado['valid']=True
            resultado['reason']=(
                f"LIQ={sweep['liquidity_tf']}:{sweep['liquidity_type']}@{sweep['nivel']} -> FIRST_CAPTURE_M15@{sweep['sweep_ts']} -> "
                f"{sweep.get('post_capture_state')} -> INTENT={intent['mode']}:{direction} -> {structure['tipo']}_M15 -> DISPLACEMENT(z={resultado['momentum_z']}) -> "
                f"{resultado['zone_source']} -> RETEST_{entry_tf} -> SL={sl_info.get('sl_classe')} -> SCALP_CAUSAL_1R -> "
                f"SETUP={resultado['setup_type']}"
            )
            return resultado
        resultado['failure_reason']='LIQUIDEZ_ESTRUTURAL_ANTES_1R'
        return resultado

    resultado['tp1']=round(tp1_2r,6)
    resultado['tp1_rr']=2.0
    resultado['tp1_origem']='GESTAO_FIXA_2R_PARCIAL_BE'
    resultado['tp2']=round(tp2_3r,6)
    resultado['tp2_rr']=3.0
    resultado['tp2_origem']='GESTAO_FIXA_3R'
    resultado['tp']=resultado['tp2']
    resultado['rr']=3.0
    resultado['tp_origem']=resultado['tp2_origem']
    resultado['be_trigger']=resultado['tp1']
    resultado['be_price']=round(entry,6)
    resultado['structural_target_context']=round(float(target['nivel']),6)
    resultado['structural_target_context_origin']=f"LIQUIDEZ_ESTRUTURAL_{target['tf']}_{target['tipo']}"

    resultado['signal']=True; resultado['valid']=True
    resultado['reason']=(
        f"LIQ={sweep['liquidity_tf']}:{sweep['liquidity_type']}@{sweep['nivel']} -> FIRST_CAPTURE_M15@{sweep['sweep_ts']} -> "
        f"{sweep.get('post_capture_state')} -> INTENT={intent['mode']}:{direction} -> {structure['tipo']}_M15 -> DISPLACEMENT(z={resultado['momentum_z']}) -> "
        f"{resultado['zone_source']} -> RETEST_{entry_tf} -> SL={sl_info.get('sl_classe')} -> {resultado['tp_origem']} RR={resultado['rr']} -> "
        f"SETUP={resultado['setup_type']}"
    )
    return resultado


# ═══════════════════════════════════════════════════════════════════════
# REPLAY — avaliar_vortex_decision_layer_v2 — item aprovado do ticket.
# SOMENTE REPLAY/AUDITORIA, sem deploy, sem alterar produção. Roda o
# pipeline experimental candle a candle, causal, sem lookahead, e
# agrega funil completo + distribuição de R:R + exemplos + MFE/MAE
# causal (reaproveitando _medir_mfe_mae_janela/_agregar_mfe_mae já
# testados). Executa somente a decision layer Paper V2.2 atual.
# ═══════════════════════════════════════════════════════════════════════

KAIROS_DECISION_LAYER_V2_VERSAO = 'KAIROS V2.2 — HTF LIQUIDITY→FIRST CAPTURE→REACTION→DISPLACEMENT→M15 MSS/CHoCH/BOS→CAUSAL FVG/IFVG/OB→RETEST→STRUCTURAL SL→TP1/TP2'


def replay_vortex_decision_layer_v2(pair, dias_historico=7, janelas_mfe_mae=JANELAS_MFE_MAE_PADRAO, fim_ts_ms=None, experimental_poi_policy=None):
    """
    Replay causal completo do KAIROS V2.2 (HTF liquidity→FIRST CAPTURE→
    reaction/displacement→M15 MSS/CHoCH/BOS→causal FVG/IFVG/OB→retest→ENTRY→SL→TP1/TP2). Mesma metodologia já aprovada (fetch único por
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
        'zona_tipo_fvg': 0, 'zona_tipo_ifvg': 0, 'zona_tipo_ob': 0,
        'choch_confirmado': 0, 'choch_invalidado_antes_gatilho': 0,
        'sl_ok': 0, 'tp_ok': 0, 'sinais_validos_brutos': 0,
    }
    distribuicao_motivos = {}
    sinais_completos_brutos = []
    experimental_obstacle_blocks = [] if experimental_poi_policy else None
    experimental_poi_state = {} if experimental_poi_policy else None
    experimental_poi_audit = [] if experimental_poi_policy else None

    for i in range(MIN_M5_IDX, len(m5)):
        # Avaliamos o estado imediatamente APÓS o fecho deste M5.
        ts_corte = m5[i]['t'] + INTERVALO_MS_POR_LABEL['5']
        if ts_corte < inicio_ts_ms or ts_corte > fim_ts_ms:
            continue
        m5_ate_agora = _kairos_candles_fechados_ate(m5, '5', ts_corte)
        m15_ate_agora = _kairos_candles_fechados_ate(m15, '15', ts_corte)
        d1_ate_agora = _kairos_candles_fechados_ate(d1, 'D', ts_corte)
        if len(m15_ate_agora) < 30:
            continue

        funil['total_ciclos_avaliados'] += 1
        tf_map = {
            'MN': _kairos_candles_fechados_ate(mn, 'M', ts_corte),
            'W1': _kairos_candles_fechados_ate(w1, 'W', ts_corte),
            'D1': d1_ate_agora,
            'H4': _kairos_candles_fechados_ate(h4, '240', ts_corte),
            'H1': _kairos_candles_fechados_ate(h1, '60', ts_corte),
            'M30': _kairos_candles_fechados_ate(m30, '30', ts_corte),
            'M15': m15_ate_agora,
            'M5': m5_ate_agora,
            'M1': _kairos_candles_fechados_ate(m1, '1', ts_corte),
        }
        try:
            r = avaliar_vortex_decision_layer_v2(
                m15_ate_agora, m5_ate_agora, d1_ate_agora, candles_por_tf=tf_map,
                audit_pair=pair,
                experimental_poi_policy=experimental_poi_policy,
                experimental_poi_state=experimental_poi_state
            )
        except Exception as e:
            distribuicao_motivos[f'EXCECAO: {e}'] = distribuicao_motivos.get(f'EXCECAO: {e}', 0) + 1
            continue

        if r['bias'] in ('alta', 'baixa'):
            funil['bias_ok'] += 1
        if r['zone_top'] is not None:
            funil['zona_encontrada'] += 1
            zt=str(r['zone_type'] or '')
            if zt.startswith('FVG_'):
                funil['zona_tipo_fvg'] += 1
            elif zt.startswith('IFVG_'):
                funil['zona_tipo_ifvg'] += 1
            elif zt.startswith('OB_'):
                funil['zona_tipo_ob'] += 1
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
        if experimental_poi_audit is not None and r.get('experimental_poi_lifecycle'):
            a=dict(r['experimental_poi_lifecycle'])
            a.update({'ts_corte':ts_corte,'failure_reason':r.get('failure_reason'),'zone_type':r.get('zone_type'),
                      'zone_created_ts':r.get('zone_created_ts'),'zone_bottom':r.get('zone_bottom'),'zone_top':r.get('zone_top')})
            experimental_poi_audit.append(a)
        if experimental_obstacle_blocks is not None and r.get('failure_reason') in ('OBSTACULO_ESTRUTURAL_ANTES_2R','OBSTACULO_ESTRUTURAL_ANTES_1R'):
            o=dict(r.get('tp1_obstacle') or {})
            experimental_obstacle_blocks.append({
                'ts_corte':ts_corte,'entry_executable_ts':r.get('timestamp'),'entry':r.get('entry'),'sl':r.get('sl'),'direction':r.get('direction'),
                'risk':abs(float(r['entry'])-float(r['sl'])) if r.get('entry') is not None and r.get('sl') is not None else None,
                'obstacle':o,'obstacle_rr':r.get('rr'),'target':r.get('first_liquidity_target'),
                'thesis':(r.get('first_capture_ts'),r.get('choch_timestamp'),r.get('direction')),
                'zone':(r.get('zone_type'),r.get('zone_created_ts'),r.get('zone_bottom'),r.get('zone_top')),
                'obstacle_state_at_entry': o.get('entry_state'),
                'obstacle_raw_state': o.get('state'),
                'obstacle_created_ts': o.get('created_ts'),
                'obstacle_first_touch_ts': o.get('first_touch_ts'),
                'obstacle_mitigated_ts': o.get('mitigated_ts'),
                'obstacle_invalidated_ts': o.get('invalidated_ts'),
                'obstacle_blocks_entry': o.get('blocks_entry'),
            })

        if r['valid']:
            funil['sinais_validos_brutos'] += 1
            sinais_completos_brutos.append({**r, 'idx_m5': i, 'pair': pair})

    sinais_unicos = []
    chaves_vistas = set()
    for s in sinais_completos_brutos:
        chave = ((s['choch_timestamp'], s['direction'], s['zone_type'], s.get('zone_created_ts'), s.get('zone_bottom'), s.get('zone_top'))
                 if experimental_poi_policy else (s['choch_timestamp'], s['direction'], s['zone_type']))
        if chave not in chaves_vistas:
            chaves_vistas.add(chave)
            sinais_unicos.append(s)

    contagem_repeticoes = {}
    for s in sinais_completos_brutos:
        chave = ((s['choch_timestamp'], s['direction'], s['zone_type'], s.get('zone_created_ts'), s.get('zone_bottom'), s.get('zone_top'))
                 if experimental_poi_policy else (s['choch_timestamp'], s['direction'], s['zone_type']))
        contagem_repeticoes[chave] = contagem_repeticoes.get(chave, 0) + 1
    repeticoes_por_sinal_unico = [
        {'choch_timestamp': s['choch_timestamp'], 'direction': s['direction'], 'zone_type': s['zone_type'],
         'repeticoes': contagem_repeticoes[((s['choch_timestamp'], s['direction'], s['zone_type'], s.get('zone_created_ts'), s.get('zone_bottom'), s.get('zone_top')) if experimental_poi_policy else (s['choch_timestamp'], s['direction'], s['zone_type']))]}
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
        'versao_pipeline': KAIROS_DECISION_LAYER_V2_VERSAO,
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
            'com mapa MN/W1/D1/H4/H1/M30/M15/M5/M1 truncado causalmente. Não altera a matemática causal de MSS/CHoCH, FVG/IFVG/OB, '
            'SL/TP existentes. Mesma '
            'metodologia causal já aprovada — cada ciclo só enxerga candles com t <= ts_corte. '
            'Sinais deduplicados por (choch_timestamp, direction, zone_type) — o mesmo CHoCH pode '
            'permanecer "válido" em vários ciclos M5 consecutivos até ser invalidado.'
        ),
        'validacao_dados': {'MN': val_mn, 'W1': val_w1, 'D1': val_d1, 'H4': val_h4, 'H1': val_h1, 'M30': val_m30, 'M15': val_m15, 'M5': val_m5, 'M1': val_m1},
        'funil': funil,
        'distribuicao_motivos_todos_ciclos': distribuicao_motivos,
        'experimental_poi_audit': experimental_poi_audit,
        'experimental_obstacle_blocks': experimental_obstacle_blocks,
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


def replay_poi_lifecycle_abc_sol(dias_historico=7, fim_ts_ms=None, pair='SOLUSD', policies=None):
    """Branch-only causal A/B/C benchmark replay. Pair-selectable; no DB/Telegram writes."""
    pair=(pair or 'SOLUSD').upper()
    if fim_ts_ms is None:
        fim_ts_ms=int(time.time()*1000)
    out={}
    policies = tuple(policies or ('A_CURRENT','B_FREEZE','C_LIFECYCLE'))
    allowed = {'A_CURRENT','B_FREEZE','C_LIFECYCLE'}
    if not policies or any(p not in allowed for p in policies):
        raise ValueError(f'policies invalidas: {policies}')
    for policy in policies:
        t0=time.time()
        print(f'[POI_ABC_PROGRESS] policy={policy} phase=REPLAY_START', flush=True)
        r=replay_vortex_decision_layer_v2(pair,dias_historico=dias_historico,fim_ts_ms=fim_ts_ms,
                                          experimental_poi_policy=policy)
        print(f'[POI_ABC_PROGRESS] policy={policy} phase=REPLAY_DONE seconds={round(time.time()-t0,2)} signals={r.get("total_sinais_unicos") if isinstance(r,dict) else None}', flush=True)
        if 'erro' in r:
            out[policy]=r
            print(f'[POI_ABC_PROGRESS] policy={policy} phase=ERROR error={r.get("erro")}', flush=True)
            continue
        m5=r.get('m5_completo') or []
        # Same strict condition as before (candle.t > entry_ts), but find the
        # first future candle by binary search instead of rescanning all M5
        # candles for every signal. Trading math and the 300-candle resolver
        # are unchanged.
        m5_ts=[x.get('t',0) for x in m5]
        import bisect
        events=[]; proxy=[]
        sinais=r.get('sinais_unicos_completos',[])
        print(f'[POI_ABC_PROGRESS] policy={policy} phase=RESOLVE_START signals={len(sinais)} m5={len(m5)}', flush=True)
        for s in sinais:
            entry_ts=s.get('timestamp')
            idx=bisect.bisect_right(m5_ts,entry_ts) if entry_ts is not None else len(m5)
            future=m5[idx:idx+300]
            res=_resolver_gestao_2r_3r_be(future,s['direction'],s['entry'],s['sl'],s['tp2'],300)
            ev=res.get('resultado'); events.append(ev)
            if ev=='TP': proxy.append(3.0)
            elif ev=='SL': proxy.append(-1.0)
            elif ev=='BE': proxy.append(0.0)
        counts={k:events.count(k) for k in ('TP','SL','BE','AMBIGUO','NENHUM')}
        binary=counts['TP']+counts['SL']
        equity=0.0; peak=0.0; maxdd=0.0; streak=0; maxstreak=0
        for x in proxy:
            equity+=x; peak=max(peak,equity); maxdd=max(maxdd,peak-equity)
            if x<0: streak+=1; maxstreak=max(maxstreak,streak)
            elif x>0: streak=0
        gross_win=sum(x for x in proxy if x>0); gross_loss=-sum(x for x in proxy if x<0)
        out[policy]={
            'N':len(events),'TP':counts['TP'],'SL':counts['SL'],'BE':counts['BE'],
            'AMBIGUO':counts['AMBIGUO'],'NENHUM':counts['NENHUM'],
            'win_rate_binary_pct':round(100*counts['TP']/binary,2) if binary else None,
            'expectancy_proxy_R':round(sum(proxy)/len(proxy),4) if proxy else None,
            'profit_factor_proxy':round(gross_win/gross_loss,4) if gross_loss else (None if not gross_win else 'INF'),
            'max_drawdown_proxy_R':round(maxdd,4),'max_sl_streak':maxstreak,
            'nota_R':'PROXY conservador: TP=+3R, SL=-1R, BE=0R; parcial TP1 nao tem percentagem definida, portanto expectancy/PF monetarios exatos continuam indisponiveis.',
            'total_sinais_unicos':r.get('total_sinais_unicos'),
            'distribuicao_motivos':r.get('distribuicao_motivos_todos_ciclos'),
        }
        audit=r.get('experimental_poi_audit') or []
        event_counts={}
        transition_examples=[]
        for a in audit:
            ev=a.get('event')
            event_counts[ev]=event_counts.get(ev,0)+1
            if ev in ('INIT','KEEP','REPLACE','EXPIRED_FREEZE','EXPIRED_NO_REPLACEMENT') and len(transition_examples)<40:
                transition_examples.append(a)
        out[policy]['poi_lifecycle_audit']={
            'events':event_counts,
            'theses':len({str(a.get('thesis_id')) for a in audit}),
            'transitions_sample':transition_examples,
        }
        # Shadow autopsy of candidates rejected ONLY because an opposing
        # structural obstacle was <2R. The live gate stays untouched.
        blocks=r.get('experimental_obstacle_blocks') or []
        unique_blocks=[]; seen_blocks=set()
        for b in blocks:
            key=(b.get('thesis'),b.get('zone'),b.get('entry'),b.get('sl'),
                 (b.get('obstacle') or {}).get('tf'),(b.get('obstacle') or {}).get('tipo'),
                 (b.get('obstacle') or {}).get('nivel'))
            if key in seen_blocks: continue
            seen_blocks.add(key); unique_blocks.append(b)
        shadow_counts={'TP3':0,'SL':0,'BE':0,'AMBIGUO':0,'NENHUM':0}
        shadow_rows=[]
        obstacle_groups={}
        for b in unique_blocks:
            entry=b.get('entry'); sl=b.get('sl'); risk=b.get('risk'); direction=b.get('direction')
            if entry is None or sl is None or not risk: continue
            sign=1.0 if direction=='LONG' else -1.0
            tp3=float(entry)+sign*(3.0*float(risk))
            entry_exec_ts=b.get('entry_executable_ts') or b.get('ts_corte') or 0
            idx=bisect.bisect_right(m5_ts,entry_exec_ts)
            future=m5[idx:idx+300]
            res=_resolver_gestao_2r_3r_be(future,direction,float(entry),float(sl),tp3,300)
            ev=res.get('resultado')
            mapped='TP3' if ev=='TP' else ev
            if mapped in shadow_counts: shadow_counts[mapped]+=1
            o=b.get('obstacle') or {}
            g=f"{o.get('tf')}:{o.get('tipo')}"
            obstacle_groups[g]=obstacle_groups.get(g,0)+1

            # Limita a autopsia ao instante em que a gestao 2R/3R realmente resolve.
            # Evita atribuir ao trade atravessamentos ocorridos depois de TP/SL/BE.
            n_res=res.get('candles_ate_resolucao')
            interaction_future=future[:n_res] if n_res else future
            resolution_ts=(interaction_future[-1].get('t') if n_res and interaction_future else None)

            # Autopsia causal do obstaculo: tocou/atravessou e MFE/MAE em R.
            obstacle_level=o.get('nivel')
            obstacle_top=o.get('top')
            obstacle_bottom=o.get('bottom')
            touched=False; crossed=False; rejected=False; first_touch_ts=None
            mfe_r=0.0; mae_r=0.0
            post_touch_extreme=None
            for fc in interaction_future:
                hi=float(fc.get('h',fc.get('c',entry))); lo=float(fc.get('l',fc.get('c',entry)))
                if direction=='LONG':
                    mfe_r=max(mfe_r,(hi-float(entry))/float(risk))
                    mae_r=max(mae_r,(float(entry)-lo)/float(risk))
                    hit = obstacle_bottom is not None and hi >= float(obstacle_bottom)
                    full_cross = obstacle_top is not None and hi > float(obstacle_top)
                    if hit and not touched:
                        touched=True; first_touch_ts=fc.get('t'); post_touch_extreme=hi
                    if touched:
                        post_touch_extreme=max(post_touch_extreme or hi,hi)
                    crossed = crossed or full_cross
                else:
                    mfe_r=max(mfe_r,(float(entry)-lo)/float(risk))
                    mae_r=max(mae_r,(hi-float(entry))/float(risk))
                    hit = obstacle_top is not None and lo <= float(obstacle_top)
                    full_cross = obstacle_bottom is not None and lo < float(obstacle_bottom)
                    if hit and not touched:
                        touched=True; first_touch_ts=fc.get('t'); post_touch_extreme=lo
                    if touched:
                        post_touch_extreme=min(post_touch_extreme if post_touch_extreme is not None else lo,lo)
                    crossed = crossed or full_cross
            # A classificacao atual mede somente toque/atravessamento da zona;
            # nao chama "rejeicao" sem uma excursao objetiva para longe do POI.
            interaction='NAO_TOCADO'
            if touched: interaction='ATRAVESSADO' if crossed else 'TOCOU_SEM_ATRAVESSAR'

            # Se a liquidez estrutural seguinte nem oferece 2R, o obstaculo nao
            # e o unico gate que mataria o trade. Se oferece >=2R, este candidato
            # e gate-exclusive e serve para auditar a trava de obstaculo.
            target_rr=(b.get('target') or {}).get('rr')
            gate_class='GATE_EXCLUSIVE' if target_rr is not None and float(target_rr)>=2.0 else 'DOWNSTREAM_REJECT_ANYWAY'

            # Shadow paralelo do micro-scalp: mede 1R sem mudar a regra oficial.
            # Primeiro candle futuro que toca +1R ou SL decide; mesmo candle = ambiguo.
            tp1=float(entry)+sign*float(risk)
            micro_result='NENHUM'; micro_resolution_ts=None; micro_minutes=None
            for fc in future:
                hi=float(fc.get('h',fc.get('c',entry))); lo=float(fc.get('l',fc.get('c',entry)))
                hit_tp = hi>=tp1 if direction=='LONG' else lo<=tp1
                hit_sl = lo<=float(sl) if direction=='LONG' else hi>=float(sl)
                if hit_tp and hit_sl:
                    micro_result='AMBIGUO'; micro_resolution_ts=fc.get('t'); break
                if hit_tp:
                    micro_result='TP1R'; micro_resolution_ts=fc.get('t'); break
                if hit_sl:
                    micro_result='SL'; micro_resolution_ts=fc.get('t'); break
            entry_executable_ts=entry_exec_ts
            if micro_resolution_ts is not None and entry_executable_ts is not None:
                micro_minutes=round((int(micro_resolution_ts)-int(entry_executable_ts))/60000.0,1)

            if len(shadow_rows)<50:
                shadow_rows.append({**b,'shadow_tp3':round(tp3,6),'shadow_result':mapped,
                                    'shadow_resolution_ts':resolution_ts,'shadow_r':res.get('r_obtido'),
                                    'structural_target_rr':target_rr,'counterfactual_class':gate_class,
                                    'micro_1r_target':round(tp1,6),'micro_1r_result':micro_result,
                                    'micro_1r_resolution_ts':micro_resolution_ts,'micro_1r_minutes_from_entry':micro_minutes,
                                    'obstacle_interaction':interaction,'obstacle_touched':touched,
                                    'obstacle_crossed':crossed,'obstacle_first_touch_ts':first_touch_ts,
                                    'mfe_R':round(mfe_r,3),'mae_R':round(mae_r,3)})
        interaction_counts={}; interaction_outcomes={}
        counterfactual_counts={}; counterfactual_outcomes={}
        micro_1r_counts={}; micro_1r_gate_exclusive_counts={}
        thesis_sets={'GATE_EXCLUSIVE':set(),'DOWNSTREAM_REJECT_ANYWAY':set()}
        micro_tp_minutes=[]
        for row in shadow_rows:
            k=row.get('obstacle_interaction','NAO_TOCADO')
            interaction_counts[k]=interaction_counts.get(k,0)+1
            ko=f"{k}:{row.get('shadow_result')}"
            interaction_outcomes[ko]=interaction_outcomes.get(ko,0)+1
            cc=row.get('counterfactual_class')
            counterfactual_counts[cc]=counterfactual_counts.get(cc,0)+1
            co=f"{cc}:{row.get('shadow_result')}"
            counterfactual_outcomes[co]=counterfactual_outcomes.get(co,0)+1
            if cc in thesis_sets: thesis_sets[cc].add(str(row.get('thesis')))
            mr=row.get('micro_1r_result')
            micro_1r_counts[mr]=micro_1r_counts.get(mr,0)+1
            if cc=='GATE_EXCLUSIVE':
                micro_1r_gate_exclusive_counts[mr]=micro_1r_gate_exclusive_counts.get(mr,0)+1
                if mr=='TP1R' and row.get('micro_1r_minutes_from_entry') is not None:
                    micro_tp_minutes.append(row.get('micro_1r_minutes_from_entry'))
        # Unidade estatistica primaria = TESE, nao variante/candidato. Escolhe apenas
        # o primeiro candidato cronologicamente executavel de cada tese para impedir
        # selecao ex-post da melhor variante.
        primary_by_thesis={}
        for row in sorted(shadow_rows,key=lambda x: (x.get('entry_executable_ts') or 0)):
            tk=str(row.get('thesis'))
            if tk not in primary_by_thesis:
                primary_by_thesis[tk]=row
        primary_rows=list(primary_by_thesis.values())
        primary_gate=[x for x in primary_rows if x.get('counterfactual_class')=='GATE_EXCLUSIVE']
        primary_gate_shadow={}; primary_gate_micro={}
        for row in primary_gate:
            sr=row.get('shadow_result'); mr=row.get('micro_1r_result')
            primary_gate_shadow[sr]=primary_gate_shadow.get(sr,0)+1
            primary_gate_micro[mr]=primary_gate_micro.get(mr,0)+1

        out[policy]['obstacle_shadow_audit']={
            'blocked_cycles':len(blocks),'unique_candidates':len(unique_blocks),
            'outcomes_300_m5':shadow_counts,'obstacle_groups':obstacle_groups,
            'interaction_counts':interaction_counts,'interaction_outcomes':interaction_outcomes,
            'counterfactual_counts':counterfactual_counts,'counterfactual_outcomes':counterfactual_outcomes,
            'independent_theses_by_class':{k:len(v) for k,v in thesis_sets.items()},
            'primary_independent_theses_total':len(primary_rows),
            'primary_gate_exclusive_theses':len(primary_gate),
            'primary_gate_exclusive_shadow_2r3r':primary_gate_shadow,
            'primary_gate_exclusive_micro_1r':primary_gate_micro,
            'micro_1r_all_candidates':micro_1r_counts,
            'micro_1r_gate_exclusive':micro_1r_gate_exclusive_counts,
            'micro_1r_gate_exclusive_tp_median_minutes_from_entry':(
                sorted(micro_tp_minutes)[len(micro_tp_minutes)//2] if micro_tp_minutes else None
            ),
            'sample':shadow_rows,
            'note':'SHADOW ONLY: producao/gates intactos. Entry executavel = timestamp real do reteste devolvido pelo motor. Interacao do obstaculo termina na resolucao 2R/3R. Metricas PRIMARY usam somente o primeiro candidato executavel de cada tese, evitando selecao ex-post.'
        }
        print(f'[POI_OBSTACLE_AUDIT] policy={policy} blocked_cycles={len(blocks)} unique={len(unique_blocks)} outcomes={shadow_counts} groups={obstacle_groups} sample={shadow_rows[:12]}', flush=True)
        print(f'[POI_ABC_LIFECYCLE] policy={policy} events={event_counts} theses={out[policy]["poi_lifecycle_audit"]["theses"]} sample={transition_examples[:12]}', flush=True)
        print(f'[POI_ABC_PROGRESS] policy={policy} phase=DONE seconds={round(time.time()-t0,2)} metrics={out[policy]}', flush=True)
    return {
        'pair':pair,'dias_historico':dias_historico,'fim_ts_ms':fim_ts_ms,
        'policies':out,'production_impact':'NONE_BRANCH_ONLY',
        'causal_note':'Mesma janela e mesmo cutoff causal para A/B/C; unica variavel e lifecycle da POI.'
    }


PARES_MONITORADOS_REPLAY = [
    'BTCUSD', 'ETHUSD', 'SOLUSD', 'XRPUSD', 'LINKUSD', 'ADAUSD',
    'AVAXUSD', 'BNBUSD', 'AAVEUSD', 'NEARUSD', 'PENDLEUSD', 'INJUSD', 'ONDOUSD',
]


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


PAPER_TRADING_V2_JANELA_LOOKBACK_DIAS = 5
PAPER_TRADING_V2_EXPIRACAO_DIAS = 15
PAPER_TRADING_V2_FORWARD_WATERMARK_MS = 1789485718000
PAPER_TRADING_V2_STRATEGY_VARIANT = 'KAIROS_V2_2_TP1_TP2_DIRECTION_NEUTRAL'


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
    Migração aditiva e idempotente da telemetria V2.2.
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
        f"🏁 TP1 2R / parcial + BE: {r.get('prealert_tp1') or 'N/A'}\n"
        f"R:R TP1: {r.get('prealert_tp1_rr') if r.get('prealert_tp1') is not None else 'N/A'}\n"
        f"🎯 TP2 final 3R: {r.get('prealert_tp2') or r.get('prealert_tp')}\n"
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
        f"TP1 2R / parcial + BE: {sinal.get('tp1') or 'N/A'}\n"
        f"TP2 final 3R: {sinal['tp']}\n"
        f"R:R final: {sinal['rr']}\n"
        f"Origem TP2: {sinal['tp_origem']}\n"
        f"Estado: PENDING\n"
        f"⚠️ 100% experimental — paper trading, zero dinheiro real."
    )


def _formatar_mensagem_resultado_paper_v2(sinal_row, resultado_status, r_obtido, ts_evento_ms):
    ts_str = datetime.fromtimestamp(ts_evento_ms / 1000, tz=timezone.utc).strftime('%Y-%m-%d %H:%M UTC') if ts_evento_ms else 'N/A'
    emoji = {'TP': '✅', 'SL': '❌', 'BE': '🟰', 'AMBIGUO': '⚠️', 'EXPIRED': '⌛'}.get(resultado_status, 'ℹ️')
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
    # podem ter a tabela persistida no volume sem a coluna nova da V2.2.
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
            res = _resolver_gestao_2r_3r_be(
                candles_futuros, sinal['direction'], sinal['entry'], sinal['sl'], sinal['tp'],
                max_candles=len(candles_futuros),
            )
            evento = res['resultado']

            if evento in ('TP', 'SL', 'BE', 'AMBIGUO'):
                r_obtido = (3.0 if evento == 'TP' else (-1.0 if evento == 'SL' else None))
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
    be = [s for s in todos if s['status'] == 'BE']
    ambiguo = [s for s in todos if s['status'] == 'AMBIGUO']
    expired = [s for s in todos if s['status'] == 'EXPIRED']

    # BE é resolução neutra do restante após TP1; não entra como win nem loss
    # no win-rate binário, mas é exposto separadamente no relatório.
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

    cronologico = sorted([s for s in todos if s['status'] in ('TP', 'SL')], key=lambda s: s['choch_timestamp'])
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
        'total_sinais': total, 'pendentes': len(pendentes), 'tp': len(tp), 'sl': len(sl), 'be': len(be),
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
            '_resolver_gestao_2r_3r_be() para gestão 2R→BE→3R. BE fica separado e sem R realizado porque o tamanho da parcial ainda não foi definido. Tabela isolada '
            '(paper_trading_v2_sinais), nunca compartilhada com produção.'
        ),
    }


@explicacao_bp.route("/kairos_v2/paper_trading_tick", methods=["GET"])
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


@explicacao_bp.route("/kairos_v2/paper_trading_relatorio", methods=["GET"])
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


@explicacao_bp.route("/kairos_v2/paper_trading_export", methods=["GET"])
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

