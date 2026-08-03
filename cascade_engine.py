# ═══════════════════════════════════════════════════════════════════════
# ADIÇÃO — Suporte/Resistência D1 + H4 juntos, com confirmação M15
# ═══════════════════════════════════════════════════════════════════════
#
# COMO USAR:
# Copiar TUDO daqui pra baixo e colar no FINAL do cascade_engine.py,
# antes do bloco de comentário "INTEGRAÇÃO NO app.py" que já existe.
#
# NÃO REMOVE nada do que já existe. NÃO MUDA calcular_bandas_sr_diario()
# nem process_pair_full() — essas continuam do jeito que estão, rodando
# normalmente. Isso aqui é 100% código novo, em paralelo.
#
# O QUE FAZ:
#   1. calcular_bandas_sr_h4()      -> mesma lógica do D1, aplicada ao H4
#   2. process_pair_full_multi_tf() -> monitora zona D1 E zona H4 ao mesmo
#      tempo; quando o preço entra numa delas (ou nas duas), vigia o M15
#      esperando SWEEP (captura de liquidez) + CHoCH (distribuição
#      confirmada) antes de disparar qualquer alerta de entrada.
#      Se D1 e H4 confirmarem juntos na mesma direção, marca como
#      "Confluência D1+H4" e dá bônus de +15 no score.
#   3. banda_para_formato_scalp()   -> conversor pronto, pra usar depois
#      se você quiser que o scalp_engine.py também use essa mesma zona
#      (resolve de vez a divergência de números entre gráfico e alerta).
#
# ─────────────────────────────────────────────────────────────────────


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
