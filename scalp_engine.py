# -*- coding: utf-8 -*-
"""
explicacao_engine.py
=====================
Módulo 100% ADITIVO ao scalp_engine.py — não altera nem uma linha dele.

O que resolve:
  O scalp_engine já calcula tudo que precisamos (gates, monte_carlo,
  ichimoku, score, detalhes/votos) dentro de `resultado`, mas isso só
  vive na memória durante o ciclo — nada disso é persistido, então não
  dá pra montar um modal tipo "Por que este sinal foi gerado?" depois.

  Este módulo:
  1. Envolve cada process_pair_scalp_* (sem modificá-las) e, quando um
     sinal realmente sai, salva o payload de explicação completo numa
     tabela nova `scalp_explicacoes` — sem tocar nas tabelas existentes.
  2. Expõe 2 endpoints Flask:
       GET /scalp/sinal/<signal_id>/explicacao
       GET /scalp/sinal/ultimo/<modo>/<pair>/explicacao

COMO INTEGRAR (troca mínima no app.py / run_live_cycle):
--------------------------------------------------------------
    import explicacao_engine as ee

    ee.init_explicacao_db(DB_FILE)          # uma vez, no startup
    app.register_blueprint(ee.explicacao_bp)

    # troca as chamadas existentes:
    # scalp_engine.process_pair_scalp(...)                 -> ee.process_pair_scalp_com_explicacao(...)
    # scalp_engine.process_pair_scalp_continuacao(...)      -> ee.process_pair_scalp_continuacao_com_explicacao(...)
    # scalp_engine.process_pair_scalp_indicadores(...)      -> ee.process_pair_scalp_indicadores_com_explicacao(...)
    # scalp_engine.process_pair_scalp_rapido(...)           -> ee.process_pair_scalp_rapido_com_explicacao(...)
    # scalp_engine.process_pair_cascata_smc(...)            -> ee.process_pair_cascata_smc_com_explicacao(...)
    # scalp_engine.process_pair_scalp_antecipado_v2(...)    -> ee.process_pair_scalp_antecipado_v2_com_explicacao(...)

    # Mesma assinatura, mesmo retorno — só passa a salvar a explicação
    # quando um sinal de verdade sai. Nada mais muda no teu app.py.
--------------------------------------------------------------
"""

from __future__ import annotations

import json
import sqlite3
from typing import Optional

from flask import Blueprint, jsonify, current_app

import scalp_engine as se

TABELA_POR_MODO = se.MODOS_SCALP  # reaproveita o dict que já existe no teu engine


# =========================================================================
# 1. PERSISTENCIA
# =========================================================================

def init_explicacao_db(db_file: str) -> None:
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


def _gates_para_filtros(gates: Optional[list]) -> list:
    return [
        {'nome': g['nome'], 'passou': g['passou'], 'descricao': g.get('detalhe', '')}
        for g in (gates or [])
    ]


def build_explicacao_payload(resultado: dict, modo: str, regime_info: Optional[tuple] = None) -> dict:
    """Monta o payload no formato do modal 'Por que este sinal foi gerado?'
    a partir do `resultado` que o scalp_engine já devolve."""

    direcao = resultado.get('direcao')
    entry, sl, tp = resultado.get('entry'), resultado.get('sl'), resultado.get('tp')
    rr = None
    if entry and sl and tp:
        risco = abs(entry - sl)
        rr = round(abs(tp - entry) / risco, 2) if risco else None

    indicadores = resultado.get('indicadores') or {}
    monte_carlo = indicadores.get('monte_carlo')
    ichimoku = indicadores.get('ichimoku')

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

    payload = {
        'modo': modo,
        'pair': resultado.get('pair'),
        'direcao': direcao,
        'motivos_principais': motivos,
        'contexto_de_mercado': {
            'regime': regime_info[0].upper() if regime_info else None,
            'adx': regime_info[1] if regime_info else indicadores.get('adx14'),
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
            'rr': rr,
            'rsi14': resultado.get('rsi14') or indicadores.get('rsi14'),
        },
        'filtros_de_qualidade': _gates_para_filtros(resultado.get('gates')),
        'score_total': {
            'score': score,
            'votos_favor': votos_favor,
            'votos_total': votos_total,
        },
        # breakdown_score: detalhes do compute_score (nome, pts) OU votos_detalhe (nome, voto)
        'breakdown_score': resultado.get('detalhes') or resultado.get('votos_detalhe') or [],
    }

    if monte_carlo:
        # já vem pronto do scalp_engine: prob_alta_pct, prob_baixa_pct, cenarios
        payload['monte_carlo'] = monte_carlo
    if ichimoku:
        payload['ichimoku'] = {**ichimoku, 'informativo': True}

    return payload


def salvar_explicacao_ultimo_sinal(db_file: str, modo: str, pair: str, resultado: dict,
                                    regime_info: Optional[tuple] = None) -> Optional[str]:
    """Se o `resultado` indica que um sinal real saiu (entrada confirmada ou
    sinal=True), busca o id que o scalp_engine acabou de gravar na tabela
    dele e salva a explicação completa amarrada nesse mesmo id."""

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

        payload = build_explicacao_payload(resultado, modo, regime_info=regime_info)

        with sqlite3.connect(db_file) as conn:
            conn.execute(
                "INSERT OR REPLACE INTO scalp_explicacoes "
                "(signal_id, pair, modo, created_at, payload_json) VALUES (?, ?, ?, ?, ?)",
                (signal_id, pair, modo, created_at, json.dumps(payload, ensure_ascii=False)),
            )
            conn.commit()
        return signal_id
    except Exception as e:
        print(f"[explicacao_engine] erro ao salvar explicacao ({modo}, {pair}): {e}")
        return None


# =========================================================================
# 2. WRAPPERS — mesma assinatura das funções originais, mesmo retorno.
#    Só acrescentam o passo de salvar a explicação quando sai sinal.
# =========================================================================

def process_pair_scalp_com_explicacao(db_file, pair, d1_candles, exec_candles, exec_tf_label,
                                       send_telegram_fn=None, h4_candles=None):
    resultado = se.process_pair_scalp(db_file, pair, d1_candles, exec_candles, exec_tf_label,
                                       send_telegram_fn, h4_candles)
    regime_info = se.compute_market_regime(d1_candles)
    salvar_explicacao_ultimo_sinal(db_file, 'normal_choch', pair, resultado, regime_info=regime_info)
    return resultado


def process_pair_scalp_continuacao_com_explicacao(db_file, pair, d1_candles, exec_candles, exec_tf_label,
                                                    send_telegram_fn=None, h4_candles=None):
    resultado = se.process_pair_scalp_continuacao(db_file, pair, d1_candles, exec_candles, exec_tf_label,
                                                   send_telegram_fn, h4_candles)
    regime_info = se.compute_market_regime(d1_candles)
    salvar_explicacao_ultimo_sinal(db_file, 'continuacao_bos', pair, resultado, regime_info=regime_info)
    return resultado


def process_pair_scalp_indicadores_com_explicacao(db_file, pair, exec_candles, exec_tf_label,
                                                     send_telegram_fn=None, d1_candles=None):
    resultado = se.process_pair_scalp_indicadores(db_file, pair, exec_candles, exec_tf_label,
                                                   send_telegram_fn, d1_candles)
    salvar_explicacao_ultimo_sinal(db_file, 'confluencia_indicadores', pair, resultado)
    return resultado


def process_pair_scalp_rapido_com_explicacao(db_file, pair, d1_candles, exec_candles, exec_tf_label,
                                               send_telegram_fn=None):
    resultado = se.process_pair_scalp_rapido(db_file, pair, d1_candles, exec_candles, exec_tf_label,
                                              send_telegram_fn)
    salvar_explicacao_ultimo_sinal(db_file, 'scalp_rapido', pair, resultado)
    return resultado


def process_pair_cascata_smc_com_explicacao(db_file, pair, w_candles, d1_candles, h4_candles, h1_candles,
                                              exec_candles, exec_tf_label, send_telegram_fn=None):
    resultado = se.process_pair_cascata_smc(db_file, pair, w_candles, d1_candles, h4_candles, h1_candles,
                                             exec_candles, exec_tf_label, send_telegram_fn)
    salvar_explicacao_ultimo_sinal(db_file, 'cascata_smc', pair, resultado)
    return resultado


def process_pair_scalp_antecipado_v2_com_explicacao(db_file, pair, d1_candles, exec_candles, exec_tf_label,
                                                       send_telegram_fn=None, h4_candles=None):
    resultado = se.process_pair_scalp_antecipado_v2(db_file, pair, d1_candles, exec_candles, exec_tf_label,
                                                     send_telegram_fn, h4_candles)
    salvar_explicacao_ultimo_sinal(db_file, 'antecipado_v2', pair, resultado)
    return resultado


# =========================================================================
# 3. BLUEPRINT FLASK
# =========================================================================

explicacao_bp = Blueprint("explicacao_bp", __name__)


def _db_file() -> str:
    return current_app.config.get('DB_FILE') or current_app.config.get('DB_PATH', '/data/alerts.db')


@explicacao_bp.route("/scalp/sinal/<signal_id>/explicacao", methods=["GET"])
def explicacao_por_id(signal_id):
    try:
        with sqlite3.connect(_db_file()) as conn:
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
        with sqlite3.connect(_db_file()) as conn:
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
