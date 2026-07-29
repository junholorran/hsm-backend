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
# ── MODO "REJEIÇÃO ANTECIPADA v2" (aditivo, seção mais abaixo) ──
# Segundo modo, mais agressivo, que roda em PARALELO ao modo acima. Não
# espera CHoCH confirmar — dispara quando um pavio varre LIQUIDEZ ANTIGA
# real (fundo/topo estabelecido) na borda da zona D1 E o RSI está extremo
# (<=20 ou >=80) naquele candle. Regra fechada (tudo ou nada, não é score
# gradual): zona + liquidez antiga + RSI extremo, os 3 juntos, ou nada.
# Divergência de RSI (Camada 16 do prompt principal) é BÔNUS de confiança,
# não obrigatória — soma ao texto/score da mensagem, não bloqueia sinal.
#
# ── MODO "CONFLUÊNCIA DE INDICADORES" (aditivo, seção mais abaixo) ──
# Terceiro modo, roda em paralelo, SEM depender de zona D1/sweep/CHoCH.
# Vota entre 10 indicadores técnicos e dispara com maioria qualificada.
#
# ── MODO "CONTINUAÇÃO (BOS)" — NOVO, aditivo, seção mais abaixo ──
# Quarto modo. Os três modos acima só entram em REVERSÃO (contra a
# direção do sweep). Esse modo cobre o caso oposto, pedido pelo Juninho
# depois de ver um caso real no gráfico: preço varre liquidez na borda
# da zona D1, mas em vez de virar (CHoCH), ROMPE ainda mais na MESMA
# direção do sweep — fechamento além do próprio nível varrido, o que é
# um BOS de continuação (Break of Structure), não uma mudança de caráter.
# Isso indica que a tese de reversão falhou e o movimento vai continuar.
# A entrada é no retorno (retest) da FVG/OB/iFVG/Breaker deixado por esse
# BOS, sempre A FAVOR da direção original do sweep (nunca contra).
# Reaproveita a mesma detecção de zona D1, mesmo cálculo de score e
# indicadores técnicos, mesma lógica de FVG/OB/iFVG/Breaker e mesmo
# cooldown — só troca "espera reversão" por "espera continuação".
# Tabelas próprias (scalp_zone_state_continuacao, scalp_signal_state_
# continuacao), nunca mistura com os outros 3 modos.
# ─────────────────────────────────────────────────────────────────────────

import sqlite3
import time
import random
from datetime import datetime, timezone, timedelta

SCORE_THRESHOLD_SINAL = 75
COOLDOWN_SECONDS = 45 * 60  # 45min — meio-termo da faixa 30-60min do Vortex
TOLERANCIA_CLUSTER_PCT = 0.006   # 0.6% — mesma tolerância usada no cascade pra clusterizar toques
MIN_EVENTOS_BANDA = 2            # mínimo de toques pra uma banda D1 ser considerada válida
MIN_FVG_GAP_PCT = 0.0005         # 0.05% do preço — FVG menor que isso é ruído, não conta como zona de entrada
MIN_CANDLE_BODY_RATIO = 0.35     # candle de confirmação precisa ter corpo >= 35% do range total (afrouxado de 50% — pedido explícito pra disparar mais fácil, depois de ver setup real de score 92 ser bloqueado por esse filtro)
STOP_BUFFER_PCT = 0.001          # 0.1% de folga além do nível estrutural — stop nunca fica colado no pavio exato
D1_LOOKBACK_DIAS = 45            # bandas D1 do Scalp olham só os últimos 45 dias — estrutura recente, não swing de meses atrás
ZONA_FORTE_TOLERANCIA_PCT = 0.0015  # 0.15% — bem mais apertada que a zona D1 (0.6%), pro modo Scalp Rápido
ZONA_FORTE_MIN_TOQUES = 3           # "liquidez forte" de verdade — mais rigoroso que o mínimo de 2 dos outros modos
SCALP_RAPIDO_COOLDOWN_SECONDS = 5 * 60  # 5min — bem mais curto, permite reentrada rápida se tomar stop
ZONA_MOVEL_LOOKBACK = 20                # candles recentes (TF de execução) pra formar a região móvel
ZONA_MOVEL_MAX_LARGURA_PCT = 0.01       # 1% — região precisa estar "apertada" (lateralizada) pra disparar
SWING_LOOKBACK = 5               # candles de cada lado pra confirmar swing high/low no TF de execução
SWEEP_MEMORY_MAX_AGE_SECONDS = 12 * 3600  # sweep salvo expira depois de 12h sem confirmar CHoCH/BOS

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
        # ── NOVO: tabela pro modo "Confluência de Indicadores" — sinal
        # baseado só nos indicadores técnicos (MACD, ADX, EMAs, Stochastic,
        # RSI, Bollinger, VWAP, Ichimoku, Monte Carlo, Candle Pattern),
        # SEM depender da sequência ICT (zona→sweep→CHoCH) estar completa.
        # Roda em paralelo aos outros modos, procurando setup o tempo
        # todo baseado só na confluência entre os indicadores. ──
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
        # ── NOVO: tabelas do modo "Continuação (BOS)" — mesmo shape das
        # tabelas do modo normal (scalp_zone_state / scalp_signal_state),
        # só que isoladas, pra nunca misturar estado de reversão com
        # estado de continuação do mesmo par. ──
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
        # ── NOVO: tabela do MODO SOMBRA DOS FILTROS — registra quando a
        # versão SEM os filtros novos (candle decisivo, alinhamento D1/H4,
        # FVG mínima, janela de 45 dias) chegaria a um sinal válido
        # (score>=75) que a versão COM filtro bloqueou. Não manda
        # Telegram, não conta cooldown, é só observação pra comparar
        # depois de alguns dias se os filtros estão cortando sinal bom
        # junto com o ruído ou não. Coluna `resultado` (pendente/win/
        # loss/expirado) é preenchida depois, ciclo a ciclo, conferindo
        # se o preço bateu TP ou SL primeiro — é isso que transforma
        # "quantas vezes bloqueou" em "bloqueou sinal bom ou ruim". ──
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
        # ── NOVO: tabela do modo "Scalp Rápido — Liquidez Forte". Sem
        # CHoCH, sem zona D1 por cluster — usa a zona diária MÓVEL
        # (réplica exata do indicador Pine Script do Juninho: pavio
        # superior/inferior do candle D1 anterior). Regra fechada:
        # zona diária + sweep + candle decisivo + lateralização
        # confirmada = dispara na hora. Cooldown bem mais curto (5min),
        # pensado pra reentrar rápido se tomar stop, já que o stop é
        # sempre curto (no pavio do sweep). ──
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
        # ── NOVO: tabela do modo "Cascata SMC" — Semanal→Diário→4H→1H
        # todos alinhados (bias calculado via lógica real do LuxAlgo,
        # não pivô centrado) → sweep na zona diária → confirmação por
        # CHoCH ou BOS (o que vier primeiro) → preço em Discount (long)
        # ou Premium (short), nunca comprando topo/vendendo fundo. ──
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
        # ── NOVO: tabela de ESTADO da cascata (memória de sweep) — mesmo
        # schema de scalp_zone_state, reaproveitando _load_saved_state e
        # _save_zone_state (que já aceitam `table` customizada). Sem
        # isso, achado em teste: o sweep "some" assim que os candles de
        # confirmação empurram ele pra fora da janela de 10 candles do
        # detect_sweep_zona_diaria_movel — exatamente o mesmo problema
        # que os Modos 1/2 já resolveram há mais tempo. ──
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
        # ── MODO SOMBRA-like: coluna nova pra registar se houve divergência
        # de RSI confirmada no sinal antecipado. Aditivo via ALTER TABLE,
        # idempotente (ignora erro se já existir). ──
        try:
            cursor.execute("ALTER TABLE scalp_antecipado_signal_state ADD COLUMN divergencia_rsi INTEGER DEFAULT 0")
            conn.commit()
        except Exception:
            pass
        # ── idempotente: se a tabela scalp_filtros_shadow já existia de
        # antes (deploy anterior sem essa coluna), adiciona agora. ──
        try:
            cursor.execute("ALTER TABLE scalp_filtros_shadow ADD COLUMN resultado TEXT DEFAULT 'pendente'")
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
def compute_d1_zones(d1_candles, lookback_dias=D1_LOOKBACK_DIAS):
    """Agrupa highs/lows do D1 em bandas (cluster), tolerância percentual,
    mínimo de toques pra validar.

    ── janela de 45 dias limita a busca de swings — pra scalp, uma banda
    formada por um swing de 3-4 meses atrás não tem muita relevância pro
    que o preço está fazendo agora. Isso faz a zona "acompanhar" o
    mercado: conforme dias antigos saem da janela, bandas baseadas só
    neles deixam de aparecer. Se `d1_candles` tiver menos candles que a
    janela (par novo, poucos dados), usa tudo o que tiver — não quebra
    por falta de histórico.

    ── NOVO: cada banda agora carrega `ultimo_toque_ts` — o timestamp do
    swing mais recente que formou aquele cluster. Isso é o que permite
    `find_active_zone` priorizar a banda mais RECENTE quando o preço
    está dentro de mais de uma zona sobreposta, em vez de pegar a
    primeira que aparecer (que podia ser um suporte de 3 semanas atrás
    ainda "válido" tecnicamente, mas bem menos relevante que um formado
    ontem ou hoje). ──"""
    if lookback_dias and len(d1_candles) > lookback_dias:
        d1_candles = d1_candles[-lookback_dias:]

    swings = []
    lb = 2
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
            if diff_pct <= TOLERANCIA_CLUSTER_PCT:
                g['pontos'].append(s['valor'])
                g['nivel'] = sum(g['pontos']) / len(g['pontos'])
                g['timestamps'].append(s['t'])
                colocado = True
                break
        if not colocado:
            grupos.append({'nivel': s['valor'], 'pontos': [s['valor']], 'timestamps': [s['t']]})

    bandas = []
    for g in grupos:
        if len(g['pontos']) >= MIN_EVENTOS_BANDA:
            largura = g['nivel'] * TOLERANCIA_CLUSTER_PCT
            bandas.append({
                'top': g['nivel'] + largura,
                'bottom': g['nivel'] - largura,
                'toques': len(g['pontos']),
                'ultimo_toque_ts': max(g['timestamps']),
            })
    return bandas


def find_active_zone(bandas, preco_atual):
    """
    Devolve a banda D1 que contém o preço atual. Se mais de uma banda
    sobreposta contiver o preço (raro, mas possível perto de bordas
    próximas), prioriza a com `ultimo_toque_ts` mais RECENTE — suporte/
    resistência recente pesa mais que um antigo ainda tecnicamente
    dentro da janela de 45 dias.
    """
    candidatas = [b for b in bandas if b['bottom'] <= preco_atual <= b['top']]
    if not candidatas:
        return None
    return max(candidatas, key=lambda b: b.get('ultimo_toque_ts', 0))


def compute_zona_diaria_movel(d1_candles):
    """
    NOVO — réplica EXATA do indicador Pine Script do Juninho ("Zonas
    Diarias Moveis (1D para 15m)"). Não usa cluster nem tolerância
    nenhuma — é geometria pura do candle D1 anterior (o último dia já
    fechado):

    - RESISTÊNCIA = zona entre o topo do CORPO (max(open,close)) e a
      MÁXIMA (high) — ou seja, o pavio superior do candle de ontem.
    - SUPORTE = zona entre a MÍNIMA (low) e o fundo do CORPO
      (min(open,close)) — o pavio inferior do candle de ontem.

    É "móvel" porque, conforme cada novo dia fecha, o candle de
    referência muda — sem precisar de cluster de toques repetidos, sem
    tolerância em %, sem chute nenhum. É a mesma lógica do
    `request.security(..., "D", [high[1], low[1], open[1], close[1]])`
    do Pine: pega o candle D1 já fechado (não o que ainda está se
    formando).

    `d1_candles[-1]` é tratado como o candle D1 do dia ATUAL (ainda em
    formação, incompleto) — por isso usamos `d1_candles[-2]` como "o
    candle de ontem", equivalente ao offset [1] do Pine Script.
    Retorna None se não houver candles suficientes.
    """
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
    """
    NOVO — pro modo "Scalp Rápido — Liquidez Forte". Mesma lógica de
    cluster do `compute_d1_zones` (swing highs/lows do D1, mesma janela
    de 45 dias), mas com duas diferenças propositais:

    1. Tolerância bem mais apertada (0.15% padrão, vs 0.6% da zona D1
       normal) e exige pelo menos 3 toques (vs 2 dos outros modos) —
       "liquidez forte" de verdade, não qualquer nível que bateu duas
       vezes por coincidência.
    2. A zona continua sendo calculada no D1 — igual os outros modos —
       só o GATILHO (sweep) é procurado depois no TF de execução
       (M5/M15), não a zona em si. "A região é sempre no diário, desce
       pro 15min só pra procurar o gatilho", como o Juninho definiu.

    Estrutura idêntica ao `compute_d1_zones`, só muda tolerância e
    mínimo de toques — mantida como função separada (não um parâmetro
    a mais em compute_d1_zones) pra deixar claro que são conceitos
    diferentes: zona D1 "normal" pros modos 1-4, zona D1 "forte" só
    pro Scalp Rápido.
    """
    if lookback_dias and len(d1_candles) > lookback_dias:
        d1_candles = d1_candles[-lookback_dias:]

    swings = []
    lb = 2
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
    """
    NOVO — "região móvel" (estilo Canal Donchian): pega os últimos
    `lookback` candles (do TF de execução) e usa a máxima e a mínima
    deles como topo/fundo da zona. Recalculada em TODO ciclo, o que faz
    ela se mover sozinha:

    - Se um candle novo deixa um pavio além do que já existia, a zona
      AUMENTA na hora pra incluir esse novo extremo.
    - Conforme candles antigos (que tinham os extremos atuais) saem da
      janela de `lookback`, e ninguém mais repete aquele nível, a zona
      DIMINUI — fica mais apertada.

    Isso é exatamente o comportamento visto no exemplo do AAVE: a
    região vai se ajustando sozinha à consolidação real, sem depender
    de "toques repetidos dentro de tolerância" feito no cluster D1 —
    aqui é simplesmente onde o preço andou nos últimos N candles.

    Diferente da zona por cluster, essa SEMPRE existe (nunca fica sem
    zona, mesmo que o preço não tenha repetido nível nenhum) — resolve
    o problema de o sistema "ficar cego" quando o preço sai de
    qualquer zona conhecida.

    Retorna também `largura_pct` — a largura da zona como % do ponto
    médio, que serve pra medir se o preço está de fato "lateralizado
    apertado" (zona estreita) ou "andando largo" (zona ampla, sem
    consolidação real).
    """
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


# ── SWEEP + CHoCH no timeframe de execução ──────────────────────────────
def compute_lux_structure_bias(candles, swing_size=50):
    """
    NOVO — réplica da lógica REAL de estrutura do LuxAlgo (função
    `leg()` + swing pivot + cruzamento, direto do Pine Script que o
    Juninho mandou). Usada na Cascata SMC pra calcular o bias de
    qualquer timeframe (Semanal, D1, H4, H1).

    Diferente do `compute_bias_from_swings` (que usa pivô centrado, mais
    lento pra confirmar — foi a causa do CHoCH do BTC ficar "preso"
    antes), essa versão usa a mesma lógica de "leg" do LuxAlgo:

    1. `leg`: a cada candle, compara a máxima/mínima de `swing_size`
       candles atrás com a máxima/mínima da janela recente. Se a máxima
       de trás for MAIOR que a máxima recente, a perna vira "bearish"
       (formando um topo); se a mínima de trás for MENOR que a mínima
       recente, a perna vira "bullish" (formando um fundo).
    2. Toda vez que a perna muda, isso confirma um pivô (high quando
       virou bearish, low quando virou bullish) `swing_size` candles
       atrás.
    3. O bias muda pra 'alta' quando o fechamento cruza ACIMA do último
       pivô de topo confirmado, e pra 'baixa' quando cruza ABAIXO do
       último pivô de fundo confirmado — exatamente o `displayStructure()`
       do LuxAlgo (CHoCH/BOS via crossover/crossunder do close).

    Retorna 'alta', 'baixa' ou 'neutro' (sem dado suficiente ou sem
    nenhum cruzamento ainda).
    """
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
            current_leg = 0  # BEARISH_LEG — formando topo
        elif low_back < lowest:
            current_leg = 1  # BULLISH_LEG — formando fundo
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
    """
    NOVO — réplica simplificada do `drawPremiumDiscountZones` do
    LuxAlgo: usa o range recente (mesma janela Donchian do
    `compute_zona_movel`) e divide ao meio (Equilibrium). Acima do
    meio = Premium (zona de venda), abaixo = Discount (zona de compra).
    Regra de ouro: nunca compra em Premium, nunca vende em Discount.
    """
    donch = compute_zona_movel(exec_candles, lookback)
    if not donch:
        return None
    equilibrium = (donch['top'] + donch['bottom']) / 2
    return {'top': donch['top'], 'bottom': donch['bottom'], 'equilibrium': equilibrium}


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
    """Depois do sweep, procura quebra de estrutura na direção OPOSTA
    (CHoCH real de reversão, confirmado por fechamento, não só pavio).

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


def detect_bos_continuation_after_sweep(exec_candles, sweep):
    """
    NOVO — contraparte do `detect_choch_after_sweep`, mas pra CONTINUAÇÃO
    em vez de reversão.

    Depois do sweep, em vez de esperar o preço virar (CHoCH), procura o
    preço ROMPER de vez, por FECHAMENTO, além do próprio nível do sweep,
    na MESMA direção que ele já estava indo antes do sweep acontecer.

    Isso é um BOS (Break of Structure) de continuação: a liquidez que foi
    varrida na borda da zona D1 não segurou o preço — ele engoliu aquela
    liquidez e seguiu no mesmo sentido, o que invalida a tese de reversão
    e confirma que o movimento anterior continua.

    - sweep['lado'] == 'baixa' (pavio varreu abaixo da zona, fechou
      de volta acima — parecia setup de reversão pra alta) -> BOS de
      continuação = um candle POSTERIOR fecha ABAIXO do próprio nível
      do sweep (sweep['nivel']), ou seja, o preço voltou a cair além de
      onde tinha varrido, sem nunca ter confirmado a reversão.
    - sweep['lado'] == 'alta' (pavio varreu acima da zona, fechou de
      volta abaixo — parecia setup de reversão pra baixa) -> BOS de
      continuação = um candle POSTERIOR fecha ACIMA do próprio nível
      do sweep, o preço continuou subindo além de onde tinha varrido.

    Retorna dict no mesmo formato do CHoCH ({'index','direcao','nivel','t'})
    pra poder reaproveitar direto as mesmas funções de FVG/OB/iFVG/Breaker
    e o mesmo compute_score — só muda o SENTIDO da direção resultante,
    que aqui é a MESMA do sweep, não a oposta.
    """
    for i, c in enumerate(exec_candles):
        if c['t'] <= sweep['t']:
            continue
        if sweep['lado'] == 'baixa' and c['c'] < sweep['nivel']:
            return {'index': i, 'direcao': 'baixa', 'nivel': sweep['nivel'], 't': c['t']}
        if sweep['lado'] == 'alta' and c['c'] > sweep['nivel']:
            return {'index': i, 'direcao': 'alta', 'nivel': sweep['nivel'], 't': c['t']}
    return None


def find_fvg_ob_after_choch(exec_candles, choch, min_gap_pct=MIN_FVG_GAP_PCT):
    """Procura FVG (3 candles) ou Order Block (última vela contrária antes
    do impulso) formado pelo próprio movimento do CHoCH/BOS.

    Nome mantido `choch` por compatibilidade — na prática recebe qualquer
    evento estrutural com o formato {'index','direcao'}, seja CHoCH
    (reversão) ou BOS de continuação; a lógica de achar FVG/OB é idêntica
    nos dois casos, só muda o que gerou o evento.

    ── FILTRO DE QUALIDADE (min_gap_pct, padrão MIN_FVG_GAP_PCT): gaps
    menores que isso são ruído/arredondamento, não uma FVG real
    negociável. Em vez de aceitar o primeiro gap que aparecer (por menor
    que seja), pula os pequenos demais e continua procurando um gap com
    tamanho relevante dentro da mesma janela — só cai pro fallback de
    Order Block se NENHUM gap na janela passar do tamanho mínimo.
    `min_gap_pct=0` desliga o filtro (usado pelo modo sombra, pra
    comparar com/sem essa trava). ──"""
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

    # fallback: Order Block = última vela contrária antes do candle do evento
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
    evento estrutural (CHoCH ou BOS) for ROMPIDO de vez (preço fecha
    totalmente do outro lado dele, não só toca) e depois volta e REJEITA
    naquele mesmo nível, o gap inverte de papel — vira zona de entrada
    com sentido invertido do que era antes, mas SEMPRE na mesma direção
    do evento que o originou. É um fallback do FVG/OB normal — só entra
    em ação se aquele não servir mais (rompido de vez)."""
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
        # REJEITAR na direção original do evento — isso confirma o iFVG
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
    movimento do evento estrutural (CHoCH ou BOS) e depois o preço volta
    e RESPEITA aquele nível na direção do evento — sequência ICT clássica
    de 3 passos: (1) OB original (cor oposta à direção do evento), (2)
    rompimento total desse OB (fechamento do outro lado, não só pavio),
    (3) retorno respeitando o nível na nova direção. Fallback final — só
    entra em ação depois de FVG/iFVG/OB simples não servirem."""
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
        # por um candle entre ele e o evento estrutural
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
        # do evento — isso confirma o breaker block
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


def candle_e_decisivo(candle, min_body_ratio=MIN_CANDLE_BODY_RATIO):
    """
    NOVO — filtro de qualidade do candle de confirmação (o candle atual,
    no momento em que o preço já está dentro da zona de entrada).

    Um candle com pavio enorme e corpo pequeno é indecisão do mercado —
    "deixou dúvida", não confirmação. Só considera o retorno à FVG/OB
    válido se o candle atual tiver corpo real: pelo menos
    `min_body_ratio` (padrão 50%) do range total (high-low) dele.

    Candle de range zero (high == low, caso raríssimo/erro de dado) não
    é bloqueado — não dá pra avaliar decisão nele, então passa direto
    pra não travar o sistema por causa de um dado degenerado.
    """
    range_total = candle['h'] - candle['l']
    if range_total <= 0:
        return True
    corpo = abs(candle['c'] - candle['o'])
    return (corpo / range_total) >= min_body_ratio


def aplicar_buffer_stop(nivel, direcao, buffer_pct=STOP_BUFFER_PCT):
    """
    NOVO — regra fixa de segurança pro stop: nunca fica exatamente
    colado no nível estrutural (pavio do sweep ou swing), sempre com uma
    folga extra na direção que dá mais espaço:

    - LONG (direcao='alta'): o nível de referência é um fundo (sweep de
      baixa ou swing low) — stop fica um pouco ABAIXO dele, dá mais
      espaço pro preço respirar sem ser varrido por ruído/spread no
      exato pavio.
    - SHORT (direcao='baixa'): o nível de referência é um topo (sweep de
      alta ou swing high) — stop fica um pouco ACIMA dele, mesmo
      raciocínio invertido.

    `buffer_pct` é uma fração do preço (0.001 = 0.1%). Aplicado em cima
    do NÍVEL, não da entrada — o stop sempre respeita a estrutura real,
    só com uma margem extra além dela.
    """
    if direcao == 'alta':
        return nivel * (1 - buffer_pct)
    return nivel * (1 + buffer_pct)


def _load_saved_state(db_file, pair, table='scalp_zone_state'):
    """Lê o último estado salvo desse par (zona/sweep/CHoCH ou BOS), pra
    decidir se dá pra reaproveitar um sweep já detectado em ciclo
    anterior. `table` permite reaproveitar essa mesma função pros modos
    normal e continuação, que guardam estado em tabelas separadas."""
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
    """Um sweep salvo continua valendo se: a zona D1 não mudou de forma
    relevante desde que foi salvo, e não passou tempo demais sem CHoCH/BOS
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

    O parâmetro `choch` é genérico: aceita tanto um evento de CHoCH
    (reversão, modo normal) quanto um evento de BOS (continuação, modo
    novo) — o formato do dict é o mesmo ({'index','direcao',...}), e o
    cálculo de score não precisa saber qual dos dois é, só usa a direção
    e o índice do candle que confirmou a estrutura.

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

    # killzone — só soma se o sweep+estrutura aconteceram dentro da killzone
    if na_killzone:
        score += 10
        detalhes.append(('dentro_killzone', 10))

    # sweep + confirmação estrutural (CHoCH ou BOS)
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

    # volume do candle de CHoCH/BOS acima da média recente = confirma força real
    vols = [c.get('v', 0) for c in exec_candles[-20:]]
    if vols:
        media_vol = sum(vols) / len(vols)
        choch_candle = exec_candles[choch['index']] if choch['index'] < len(exec_candles) else None
        if choch_candle and choch_candle.get('v', 0) > media_vol * 1.3:
            score += 8
            detalhes.append(('volume_choch_forte', 8))

    # ── indicadores técnicos pesando de verdade no score, não só
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

    # Bollinger: preço no candle do sweep tocou/passou a banda oposta à
    # direção = movimento esticado o suficiente pra justificar entrada
    # real, não só ruído dentro do canal
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


def process_pair_scalp(db_file, pair, d1_candles, exec_candles, exec_tf_label, send_telegram_fn=None, h4_candles=None):
    """Orquestrador do ciclo de Scalp Ao Vivo (modo NORMAL — reversão via
    CHoCH) pra 1 par. Aditivo, roda em cima dos mesmos candles já
    buscados no ciclo do Trade Ao Vivo — não faz nenhuma call extra à
    Bybit.

    `h4_candles` é opcional — usado pro FILTRO DE ALINHAMENTO COM
    TIMEFRAMES MAIORES (ver mais abaixo, logo após o CHoCH confirmar).
    Se não vier (chamada antiga sem esse parâmetro), o filtro usa só o
    D1 e não quebra nada que já estava funcionando."""
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
        'zona_ultimo_toque_ts': None,
        'sweep_nivel': None,
        'sweep_lado': None,
        'choch_nivel': None,
        'choch_direcao': None,
        'bias_d1': None,
        'bias_h4': None,
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
        # Só mostra a ÚLTIMA zona/sweep/CHoCH conhecidos (marcados como
        # não-ativos), e só deixa expirar pelo mesmo critério de sempre
        # (12h sem confirmar, ou zona mudou de verdade). ──
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
    resultado['zona_ultimo_toque_ts'] = zona.get('ultimo_toque_ts')

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

    # ── NOVO: o candle que FORMA o CHoCH também precisa ser decisivo
    # (corpo real, sem pavio grande de rejeição). Isso pega o caso onde
    # a quebra de estrutura já nasce com dúvida — ex: candle fecha acima
    # do nível de referência (confirma CHoCH de alta), mas deixa um
    # pavio grande em cima, sinal de que vendedores já estavam
    # defendendo aquele preço no mesmo movimento que deveria confirmar
    # força compradora. Isso é DIFERENTE do filtro no candle de retorno
    # (mais abaixo) — aqui é o candle que CRIA o evento estrutural,
    # antes de sequer procurar a FVG/OB. Se não for decisivo, trata como
    # se o CHoCH ainda não tivesse confirmado — próximo ciclo tenta de
    # novo com candles novos. ──
    if choch['index'] < len(exec_candles) and not candle_e_decisivo(exec_candles[choch['index']]):
        resultado['motivo'] = 'candle da quebra de estrutura (CHoCH) tem pavio grande — aguardando quebra mais decisiva'
        _save_zone_state(db_file, pair, zona, 'sweep', now, sweep=sweep)
        return resultado

    resultado['choch_nivel'] = round(choch['nivel'], 6)
    resultado['choch_direcao'] = choch['direcao']

    # ── NOVO: FILTRO DE ALINHAMENTO COM TIMEFRAMES MAIORES — não é
    # "qualquer quebra de estrutura" no M5/M15 que vale. O CHoCH só é
    # considerado válido se o bias do D1 (e do H4, quando disponível)
    # não estiver claramente CONTRA a direção dele. Reaproveita a mesma
    # `compute_bias_from_swings` já usada no modo Antecipado v2 — 'neutro'
    # sempre passa (não bloqueia), só bloqueia se o bias maior for
    # oposto de verdade. Isso pega exatamente o caso de CHoCH local que
    # não tem apoio nenhum da estrutura maior — ruído de curto prazo. ──
    bias_d1 = compute_bias_from_swings(d1_candles)
    bias_h4 = compute_bias_from_swings(h4_candles) if h4_candles else 'neutro'
    resultado['bias_d1'] = bias_d1
    resultado['bias_h4'] = bias_h4

    contra_alta = choch['direcao'] == 'alta' and (bias_d1 == 'baixa' and bias_h4 == 'baixa')  # afrouxado: só bloqueia se D1 E H4 concordarem contra
    contra_baixa = choch['direcao'] == 'baixa' and (bias_d1 == 'alta' and bias_h4 == 'alta')  # afrouxado: só bloqueia se D1 E H4 concordarem contra
    if contra_alta or contra_baixa:
        resultado['motivo'] = (
            f"CHoCH confirmado, mas contra o bias dos timeframes maiores "
            f"(D1={bias_d1}, H4={bias_h4}) — descartado"
        )
        _save_zone_state(db_file, pair, zona, 'choch_contra_bias_maior', now, sweep=sweep, choch=choch)
        return resultado

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

    # ── NOVO: preço já está na zona, mas o candle atual precisa ser
    # DECISIVO (corpo real, não pavio grande) — senão é indecisão do
    # mercado bem no momento da confirmação, não sinal claro. Não perde
    # a zona (ela continua salva/válida), só segura o disparo até um
    # candle com corpo confirmar de verdade. ──
    if not candle_e_decisivo(exec_candles[-1]):
        resultado['motivo'] = 'preço na zona de entrada, mas candle de confirmação indeciso (pavio grande) — aguardando'
        _save_zone_state(db_file, pair, zona, 'aguardando_candle_decisivo', now, sweep=sweep, choch=choch)
        return resultado

    score, detalhes = compute_score(zona, sweep, choch, entry_zone, exec_candles, na_killzone, indicadores=resultado.get('indicadores'))
    resultado['score'] = score
    resultado['detalhes'] = detalhes
    resultado['direcao'] = choch['direcao']

    sl = aplicar_buffer_stop(sweep['nivel'], choch['direcao'])
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


SHADOW_RESOLVE_MAX_AGE_HOURS = 24  # sinal shadow pendente há mais de 24h sem bater TP/SL vira 'expirado'


def _resolver_filtros_shadow_pendentes(db_file, pair, exec_candles):
    """
    NOVO — pra cada sinal do modo sombra ainda 'pendente' desse par,
    olha os candles do TF de execução DEPOIS do momento em que o sinal
    foi criado e checa se o preço bateu TP ou SL primeiro. Atualiza
    `resultado` pra 'win' ou 'loss'. Se passar de
    SHADOW_RESOLVE_MAX_AGE_HOURS sem bater nenhum dos dois, marca como
    'expirado' (não fica pendente pra sempre, e não conta nem como win
    nem como loss no relatório).

    Isso é o que transforma "esse filtro bloqueou 12 sinais" em
    "esse filtro bloqueou 12 sinais, dos quais 8 teriam dado WIN" —
    dado concreto em vez de só contagem.

    Limitação honesta: só enxerga candles dentro da janela de
    `exec_candles` recebida nesse ciclo (tipicamente ~200 candles do TF
    de execução). Se um sinal shadow for muito antigo e os candles dele
    já saíram da janela, ele fica pendente até expirar por tempo — não
    é perdido, só não resolve mais cedo.
    """
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


def process_pair_scalp_filtros_shadow(db_file, pair, d1_candles, exec_candles, exec_tf_label, h4_candles=None):
    """
    NOVO — MODO SOMBRA DOS FILTROS (modo normal/reversão). Roda a MESMA
    sequência do process_pair_scalp, mas SEM aplicar os filtros
    adicionados hoje:
      - janela de 45 dias na zona D1 (usa histórico completo aqui)
      - candle decisivo no candle que forma o CHoCH
      - FVG mínima (0.05%)
      - candle decisivo no candle de retorno/confirmação
      - alinhamento com bias D1/H4

    Se a versão SEM filtro chegar a um sinal válido (score >= 75) e pelo
    menos UM desses filtros teria bloqueado esse mesmo sinal na versão
    real, salva um registro em scalp_filtros_shadow dizendo exatamente
    qual(is) filtro(s) foi(ram) o responsável. Não manda Telegram, não
    conta pra cooldown de sinal real — é só dado pra comparação depois.

    Antes de gerar sinal novo, resolve os pendentes de ciclos anteriores
    (confere se bateram TP/SL) — ver `_resolver_filtros_shadow_pendentes`.

    Retorna o dict salvo (ou None se não havia nada relevante a
    registrar nesse ciclo — ou porque não chegou nem a formar sinal sem
    filtro, ou porque chegou e NENHUM filtro teria bloqueado, ou seja,
    os filtros não fizeram diferença nesse caso específico).
    """
    _resolver_filtros_shadow_pendentes(db_file, pair, exec_candles)

    preco_atual = exec_candles[-1]['c']

    # zona SEM limitar a 45 dias — janela completa, "como era antes"
    bandas = compute_d1_zones(d1_candles, lookback_dias=None)
    zona = find_active_zone(bandas, preco_atual)
    if not zona:
        return None

    sweep = detect_sweep_in_zone(exec_candles, zona)
    if not sweep:
        return None

    choch = detect_choch_after_sweep(exec_candles, sweep)
    if not choch:
        return None

    # entry_zone SEM filtro de tamanho mínimo de FVG
    entry_zone = find_fvg_ob_after_choch(exec_candles, choch, min_gap_pct=0)
    if not entry_zone:
        entry_zone = find_ifvg_after_choch(exec_candles, choch)
    if not entry_zone:
        entry_zone = find_breaker_block_after_choch(exec_candles, choch)
    if not entry_zone:
        return None

    if not price_in_zone(entry_zone, preco_atual):
        return None

    # score calculado igual (indicadores não são "filtro", continuam iguais)
    na_killzone, _ = is_in_killzone()
    indicadores = compute_technical_indicators(exec_candles)
    score, _ = compute_score(zona, sweep, choch, entry_zone, exec_candles, na_killzone, indicadores=indicadores)

    if score < SCORE_THRESHOLD_SINAL:
        return None

    # ── chegou a um sinal válido SEM filtro — agora checa quais filtros
    # da versão REAL teriam bloqueado esse mesmo sinal ──
    filtros_bloqueados = []

    if choch['index'] < len(exec_candles) and not candle_e_decisivo(exec_candles[choch['index']]):
        filtros_bloqueados.append('candle_choch_indeciso')

    bias_d1 = compute_bias_from_swings(d1_candles)
    bias_h4 = compute_bias_from_swings(h4_candles) if h4_candles else 'neutro'
    contra_alta = choch['direcao'] == 'alta' and (bias_d1 == 'baixa' and bias_h4 == 'baixa')  # afrouxado: só bloqueia se D1 E H4 concordarem contra
    contra_baixa = choch['direcao'] == 'baixa' and (bias_d1 == 'alta' and bias_h4 == 'alta')  # afrouxado: só bloqueia se D1 E H4 concordarem contra
    if contra_alta or contra_baixa:
        filtros_bloqueados.append(f'bias_maior_contra(D1={bias_d1},H4={bias_h4})')

    entry_zone_com_filtro = find_fvg_ob_after_choch(exec_candles, choch, min_gap_pct=MIN_FVG_GAP_PCT)
    if not entry_zone_com_filtro:
        entry_zone_com_filtro = find_ifvg_after_choch(exec_candles, choch)
    if not entry_zone_com_filtro:
        entry_zone_com_filtro = find_breaker_block_after_choch(exec_candles, choch)
    if not entry_zone_com_filtro or (
        round(entry_zone_com_filtro['top'], 6) != round(entry_zone['top'], 6)
        or round(entry_zone_com_filtro['bottom'], 6) != round(entry_zone['bottom'], 6)
    ):
        filtros_bloqueados.append('fvg_pequena_demais')

    if not candle_e_decisivo(exec_candles[-1]):
        filtros_bloqueados.append('candle_retorno_indeciso')

    bandas_45 = compute_d1_zones(d1_candles, lookback_dias=D1_LOOKBACK_DIAS)
    zona_45 = find_active_zone(bandas_45, preco_atual)
    if not zona_45:
        filtros_bloqueados.append('fora_da_janela_45_dias')

    if not filtros_bloqueados:
        # sinal já passaria com os filtros também — nada de diferença
        # relevante pra registrar nesse ciclo
        return None

    entry = preco_atual
    sl = aplicar_buffer_stop(sweep['nivel'], choch['direcao'])
    risco = abs(entry - sl)
    tp = entry + risco * 2 if choch['direcao'] == 'alta' else entry - risco * 2

    resultado = {
        'pair': pair, 'exec_tf': exec_tf_label, 'modo': 'filtros_shadow',
        'direcao': choch['direcao'], 'score': score,
        'entry': round(entry, 6), 'sl': round(sl, 6), 'tp': round(tp, 6),
        'filtros_que_bloqueariam': filtros_bloqueados,
    }
    _save_filtro_shadow(db_file, pair, exec_tf_label, choch['direcao'], score, resultado['entry'], resultado['sl'], resultado['tp'], filtros_bloqueados)
    return resultado


def filtros_shadow_report(db_file, pair=None, limit=50):
    """
    Devolve o histórico de casos em que os filtros novos teriam
    bloqueado um sinal que, sem eles, teria pontuado score >= 75 — pra
    consulta via endpoint. Agrupa também:
      - contagem por tipo de filtro (quantas vezes cada um bloqueou)
      - win rate por tipo de filtro (dos casos já resolvidos, quantos
        teriam dado WIN se o sinal tivesse passado) — é isso que
        responde "o filtro tá cortando sinal bom ou ruim?" com dado
        real, não só contagem.
    """
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
        stats_por_filtro = {}  # nome_filtro -> {'win': n, 'loss': n, 'pendente': n, 'expirado': n}
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

        # ── calcula win rate por filtro só sobre os casos JÁ resolvidos
        # (win+loss) — pendente/expirado não entram na conta pra não
        # distorcer a % com sinais que ainda nem tiveram tempo de bater
        # TP ou SL. ──
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


def process_pair_scalp_continuacao(db_file, pair, d1_candles, exec_candles, exec_tf_label, send_telegram_fn=None, h4_candles=None):
    """
    NOVO — modo "Continuação (BOS)". Mesma estrutura do
    `process_pair_scalp` (modo normal), mas em vez de esperar CHoCH
    (reversão), espera BOS (a favor da direção do sweep — continuação).

    Isso cobre o caso que o Juninho identificou no gráfico: preço varre
    liquidez na borda da zona D1, volta a testar a FVG/OB deixado por
    aquele movimento, mas em vez de reverter, CONTINUA na mesma direção
    de antes — o que o modo normal nunca conseguiria capturar, porque
    ele só entra contra a direção do sweep.

    `h4_candles` é opcional — usado pro mesmo FILTRO DE ALINHAMENTO COM
    TIMEFRAMES MAIORES do modo normal (ver logo após o BOS confirmar).

    Guarda estado em tabelas próprias (scalp_zone_state_continuacao,
    scalp_signal_state_continuacao) — nunca mistura com o modo normal,
    mesmo rodando no mesmo par ao mesmo tempo.
    """
    now = int(time.time())
    na_killzone, killzone_nome = is_in_killzone()
    preco_atual = exec_candles[-1]['c']

    bandas = compute_d1_zones(d1_candles)
    zona = find_active_zone(bandas, preco_atual)

    resultado = {
        'pair': pair,
        'exec_tf': exec_tf_label,
        'modo': 'continuacao',
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
        'zona_ultimo_toque_ts': None,
        'sweep_nivel': None,
        'sweep_lado': None,
        'bos_nivel': None,
        'bos_direcao': None,
        'bias_d1': None,
        'bias_h4': None,
        'entry_zone_top': None,
        'entry_zone_bottom': None,
        'entry_zone_tipo': None,
        'indicadores': None,
        'em_cooldown': False,
    }

    try:
        resultado['indicadores'] = compute_technical_indicators(exec_candles)
    except Exception as e:
        print(f"[scalp_engine] erro ao calcular indicadores (continuacao) de {pair}: {e}")

    if not zona:
        saved = _load_saved_state(db_file, pair, table='scalp_zone_state_continuacao')
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
    resultado['zona_ultimo_toque_ts'] = zona.get('ultimo_toque_ts')

    fresh_sweep = detect_sweep_in_zone(exec_candles, zona)
    saved = _load_saved_state(db_file, pair, table='scalp_zone_state_continuacao')
    saved_valido = _sweep_ainda_valido(saved, zona, now)

    sweep = None
    if fresh_sweep and (not saved_valido or fresh_sweep['t'] >= saved['sweep_ts']):
        sweep = fresh_sweep
    elif saved_valido:
        sweep = {'t': saved['sweep_ts'], 'nivel': saved['sweep_nivel'], 'lado': saved['sweep_lado']}

    if not sweep:
        resultado['motivo'] = 'sem sweep detectado ainda'
        _save_zone_state(db_file, pair, zona, 'zona', now, table='scalp_zone_state_continuacao')
        return resultado

    resultado['sweep_nivel'] = round(sweep['nivel'], 6)
    resultado['sweep_lado'] = sweep['lado']

    bos = detect_bos_continuation_after_sweep(exec_candles, sweep)
    if not bos:
        resultado['motivo'] = 'sweep ok, mas BOS de continuação ainda não confirmou'
        _save_zone_state(db_file, pair, zona, 'sweep', now, sweep=sweep, table='scalp_zone_state_continuacao')
        return resultado

    # ── NOVO: mesmo cuidado do modo normal — o candle que FORMA o BOS
    # também precisa ser decisivo (corpo real, sem pavio grande). Pega
    # o caso de um rompimento de continuação que já nasce com dúvida. ──
    if bos['index'] < len(exec_candles) and not candle_e_decisivo(exec_candles[bos['index']]):
        resultado['motivo'] = 'candle da quebra de estrutura (BOS) tem pavio grande — aguardando quebra mais decisiva'
        _save_zone_state(db_file, pair, zona, 'sweep', now, sweep=sweep, table='scalp_zone_state_continuacao')
        return resultado

    resultado['bos_nivel'] = round(bos['nivel'], 6)
    resultado['bos_direcao'] = bos['direcao']

    # ── NOVO: mesmo FILTRO DE ALINHAMENTO COM TIMEFRAMES MAIORES do
    # modo normal. Continuação também precisa de apoio da estrutura
    # maior — não é "qualquer BOS local" que vale, tem que fazer sentido
    # dentro do bias de D1/H4 (ou pelo menos não estar claramente
    # contra ele). ──
    bias_d1 = compute_bias_from_swings(d1_candles)
    bias_h4 = compute_bias_from_swings(h4_candles) if h4_candles else 'neutro'
    resultado['bias_d1'] = bias_d1
    resultado['bias_h4'] = bias_h4

    contra_alta = bos['direcao'] == 'alta' and (bias_d1 == 'baixa' and bias_h4 == 'baixa')  # afrouxado: só bloqueia se D1 E H4 concordarem contra
    contra_baixa = bos['direcao'] == 'baixa' and (bias_d1 == 'alta' and bias_h4 == 'alta')  # afrouxado: só bloqueia se D1 E H4 concordarem contra
    if contra_alta or contra_baixa:
        resultado['motivo'] = (
            f"BOS confirmado, mas contra o bias dos timeframes maiores "
            f"(D1={bias_d1}, H4={bias_h4}) — descartado"
        )
        _save_zone_state(db_file, pair, zona, 'bos_contra_bias_maior', now, sweep=sweep, choch=bos, table='scalp_zone_state_continuacao')
        return resultado

    entry_zone = find_fvg_ob_after_choch(exec_candles, bos)
    if not entry_zone:
        entry_zone = find_ifvg_after_choch(exec_candles, bos)
    if not entry_zone:
        entry_zone = find_breaker_block_after_choch(exec_candles, bos)
    if not entry_zone:
        resultado['motivo'] = 'BOS confirmado, mas sem FVG/OB/iFVG/Breaker de retorno ainda'
        _save_zone_state(db_file, pair, zona, 'bos', now, sweep=sweep, choch=bos, table='scalp_zone_state_continuacao')
        return resultado

    resultado['entry_zone_top'] = round(entry_zone['top'], 6)
    resultado['entry_zone_bottom'] = round(entry_zone['bottom'], 6)
    resultado['entry_zone_tipo'] = entry_zone['tipo']

    if not price_in_zone(entry_zone, preco_atual):
        resultado['motivo'] = 'preço ainda fora da zona de entrada (FVG/OB/iFVG/Breaker) — aguardando retorno'
        _save_zone_state(db_file, pair, zona, 'aguardando_retorno', now, sweep=sweep, choch=bos, table='scalp_zone_state_continuacao')
        return resultado

    # ── NOVO: mesmo filtro do modo normal — candle atual precisa ser
    # DECISIVO (corpo real, não pavio grande) antes de liberar o score. ──
    if not candle_e_decisivo(exec_candles[-1]):
        resultado['motivo'] = 'preço na zona de entrada, mas candle de confirmação indeciso (pavio grande) — aguardando'
        _save_zone_state(db_file, pair, zona, 'aguardando_candle_decisivo', now, sweep=sweep, choch=bos, table='scalp_zone_state_continuacao')
        return resultado

    score, detalhes = compute_score(zona, sweep, bos, entry_zone, exec_candles, na_killzone, indicadores=resultado.get('indicadores'))
    resultado['score'] = score
    resultado['detalhes'] = detalhes
    resultado['direcao'] = bos['direcao']

    sl = aplicar_buffer_stop(sweep['nivel'], bos['direcao'])
    entry = preco_atual
    risco = abs(entry - sl)
    tp = entry + risco * 2 if bos['direcao'] == 'alta' else entry - risco * 2
    resultado['entry'] = round(entry, 6)
    resultado['sl'] = round(sl, 6)
    resultado['tp'] = round(tp, 6)

    if score >= SCORE_THRESHOLD_SINAL:
        segundos_desde = _segundos_desde_ultimo_alerta(db_file, 'scalp_signal_state_continuacao', pair)
        em_cooldown = segundos_desde is not None and segundos_desde < COOLDOWN_SECONDS
        resultado['em_cooldown'] = em_cooldown

        if em_cooldown:
            restante_min = (COOLDOWN_SECONDS - segundos_desde) // 60
            resultado['motivo'] = f'score {score} válido, mas em cooldown ({restante_min}min restantes)'
            _save_zone_state(db_file, pair, zona, 'cooldown', now, sweep=sweep, choch=bos, table='scalp_zone_state_continuacao')
            _save_signal(db_file, pair, exec_tf_label, resultado, alerted=False, table='scalp_signal_state_continuacao')
        else:
            resultado['motivo'] = 'entrada'
            _save_zone_state(db_file, pair, zona, 'entrada', now, sweep=sweep, choch=bos, table='scalp_zone_state_continuacao')
            _save_signal(db_file, pair, exec_tf_label, resultado, alerted=True, table='scalp_signal_state_continuacao')
            if send_telegram_fn:
                arrow = '📈' if bos['direcao'] == 'alta' else '📉'
                kz_txt = f" | Killzone: {killzone_nome}" if na_killzone else " | fora da killzone"
                msg = f"🔁 <b>Sinal Scalp Continuação — {pair}</b>\n\n"
                msg += f"{arrow} <b>{'LONG' if bos['direcao']=='alta' else 'SHORT'}</b> | TF execução: {exec_tf_label}{kz_txt}\n"
                msg += f"📍 Entrada: {resultado['entry']}\n"
                msg += f"🛑 Stop: {resultado['sl']}\n"
                msg += f"✅ TP: {resultado['tp']}\n"
                msg += f"🎯 Score: {score}/100\n"
                msg += f"\n💡 Zona D1 → Sweep → BOS (continuação, a favor do sweep) → retorno {entry_zone['tipo']}"
                send_telegram_fn(msg)
    else:
        resultado['motivo'] = f'score {score} abaixo de {SCORE_THRESHOLD_SINAL} — sem entrada'
        _save_zone_state(db_file, pair, zona, 'score_insuficiente', now, sweep=sweep, choch=bos, table='scalp_zone_state_continuacao')

    return resultado


def _save_zone_state(db_file, pair, zona, fase, now, sweep=None, choch=None, table='scalp_zone_state'):
    """`table` permite reaproveitar essa função pros modos normal e
    continuação, que guardam estado em tabelas separadas mas com o
    mesmo schema."""
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


def _save_signal(db_file, pair, exec_tf_label, resultado, alerted, table='scalp_signal_state'):
    """`table` permite reaproveitar essa função pros modos normal e
    continuação, que guardam sinais em tabelas separadas mas com o
    mesmo schema."""
    try:
        prefixo = 'cont' if table == 'scalp_signal_state_continuacao' else 'scalp'
        signal_id = f"{prefixo}_{pair}_{int(time.time()*1000)}"
        with sqlite3.connect(db_file) as conn:
            cursor = conn.cursor()
            cursor.execute(f'''
                INSERT INTO {table} (id, pair, created_at, exec_tf, direcao, score, entry, sl, tp, na_killzone, alerted)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ''', (
                signal_id, pair, int(time.time()), exec_tf_label,
                resultado['direcao'], resultado['score'], resultado['entry'], resultado['sl'], resultado['tp'],
                1 if resultado['na_killzone'] else 0, 1 if alerted else 0,
            ))
            conn.commit()
    except Exception as e:
        print(f"[scalp_engine] erro ao salvar signal de {pair} ({table}): {e}")


def scalp_signal_history(db_file, pair=None, limit=30, table='scalp_signal_state'):
    """
    NOVO — histórico de sinais REAIS do Scalp Ao Vivo (modo Normal por
    padrão; passar table='scalp_signal_state_continuacao' pro histórico
    do modo Continuação). Diferente do /live/history do app.py (que lê
    a tabela `live_signals`, alimentada pelo cascade_engine) — essa
    função lê a tabela certa onde o scalp_engine de fato salva cada vez
    que `score >= SCORE_THRESHOLD_SINAL`, alertado ou não (cooldown).

    Cada linha tem: id, pair, created_at, exec_tf, direcao, score,
    entry, sl, tp, na_killzone, alerted. Não calcula resultado (win/
    loss) — só devolve o que foi salvo no momento do sinal. Pra saber
    se bateu TP ou SL depois, é preciso cruzar com o preço histórico.
    """
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
    """
    NOVO — sweep específico pras zonas do `compute_zona_diaria_movel`.

    Diferente do `detect_sweep_in_zone` genérico (que checa os dois
    lados de uma zona só), aqui são DUAS zonas distintas com papéis
    fixos, então cada uma só é testada no lado que faz sentido:

    - RESISTÊNCIA (pavio de cima de ontem): sweep = candle varre ACIMA
      da máxima de ontem (zona['top']) mas fecha de volta ABAIXO dela —
      rejeição na máxima de ontem. Isso indica reversão pra BAIXA.
    - SUPORTE (pavio de baixo de ontem): sweep = candle varre ABAIXO da
      mínima de ontem (zona['bottom']) mas fecha de volta ACIMA dela —
      rejeição na mínima de ontem. Isso indica reversão pra ALTA.

    Retorna o sweep mais recente entre os dois tipos, ou None.
    """
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


def process_pair_scalp_rapido(db_file, pair, d1_candles, exec_candles, exec_tf_label, send_telegram_fn=None):
    """
    NOVO — modo "Scalp Rápido — Liquidez Forte". Réplica do indicador
    Pine Script do Juninho ("Zonas Diarias Moveis (1D para 15m)"): a
    zona não é mais cluster de toques com tolerância chutada — é
    geometria exata do candle D1 ANTERIOR (o último dia já fechado):

    - RESISTÊNCIA = pavio superior de ontem (entre o topo do corpo e a
      máxima)
    - SUPORTE = pavio inferior de ontem (entre a mínima e o fundo do
      corpo)

    Entrada sem esperar CHoCH — dispara direto quando o preço varre
    (sweep) a máxima ou a mínima de ontem e rejeita de volta. Regra
    fechada, tudo ou nada:
    - Zona diária (resistência ou suporte) precisa existir (candle de
      ontem disponível)
    - Sweep detectado no TF de execução
    - Candle do sweep precisa ser DECISIVO (reaproveita `candle_e_decisivo`)
    - Região móvel (Donchian, ver compute_zona_movel) precisa estar
      apertada — confirma lateralização real antes de disparar
    - RSI precisa estar EXTREMO no candle do sweep (<=RSI_EXTREMO_BAIXA
      pra long, >=RSI_EXTREMO_ALTA pra short) — réplica do processo
      manual descrito: "RSI no talo, zona sendo defendida"

    Stop = pavio exato do candle do sweep + buffer de segurança. TP =
    RR 2:1. Cooldown curto (SCALP_RAPIDO_COOLDOWN_SECONDS, 5min) —
    permite reentrada rápida se o trade tomar stop.
    """
    preco_atual = exec_candles[-1]['c']

    resultado = {
        'pair': pair, 'exec_tf': exec_tf_label, 'modo': 'scalp_rapido',
        'sinal': False, 'direcao': None, 'entry': None, 'sl': None, 'tp': None,
        'zona_tipo': None, 'motivo': None, 'em_cooldown': False, 'rsi': None,
        'resistencia_top': None, 'resistencia_bottom': None,
        'suporte_top': None, 'suporte_bottom': None,
        'zona_movel_top': None, 'zona_movel_bottom': None, 'zona_movel_largura_pct': None,
    }

    zona_diaria = compute_zona_diaria_movel(d1_candles)
    if not zona_diaria:
        resultado['motivo'] = 'sem candle D1 anterior disponível ainda'
        return resultado

    resultado['resistencia_top'] = round(zona_diaria['resistencia']['top'], 6)
    resultado['resistencia_bottom'] = round(zona_diaria['resistencia']['bottom'], 6)
    resultado['suporte_top'] = round(zona_diaria['suporte']['top'], 6)
    resultado['suporte_bottom'] = round(zona_diaria['suporte']['bottom'], 6)

    # ── região MÓVEL (Donchian, ver compute_zona_movel) — sempre
    # calculada e exposta, pra acompanhar ela se ajustando sozinha.
    # Só EXIGE que esteja "apertada" como confirmação extra de
    # lateralização real antes de disparar. ──
    zona_movel = compute_zona_movel(exec_candles)
    if zona_movel:
        resultado['zona_movel_top'] = round(zona_movel['top'], 6)
        resultado['zona_movel_bottom'] = round(zona_movel['bottom'], 6)
        resultado['zona_movel_largura_pct'] = round(zona_movel['largura_pct'] * 100, 4)

    sweep = detect_sweep_zona_diaria_movel(exec_candles, zona_diaria)
    if not sweep:
        resultado['motivo'] = 'sem sweep na resistência ou suporte de ontem ainda'
        return resultado

    resultado['zona_tipo'] = sweep['tipo_zona']

    if not candle_e_decisivo(exec_candles[sweep['index']]):
        resultado['motivo'] = f"sweep na {sweep['tipo_zona']}, mas candle indeciso (pavio grande) — aguardando"
        return resultado

    # ── checa lateralização usando os candles ANTES do sweep, sem
    # incluir o próprio candle do sweep — ele naturalmente tem pavio
    # maior que o resto (é o que causa o sweep), então incluí-lo na
    # conta da região móvel alargaria ela artificialmente bem na hora
    # que mais importa checar se HAVIA consolidação real antes. ──
    candles_pre_sweep = exec_candles[:sweep['index']]
    zona_movel_pre_sweep = compute_zona_movel(candles_pre_sweep) if candles_pre_sweep else None
    if zona_movel_pre_sweep and zona_movel_pre_sweep['largura_pct'] > ZONA_MOVEL_MAX_LARGURA_PCT:
        resultado['motivo'] = (
            f"sweep e candle ok, mas região móvel (antes do sweep) larga demais "
            f"({round(zona_movel_pre_sweep['largura_pct']*100, 2)}% > {ZONA_MOVEL_MAX_LARGURA_PCT*100}%) "
            f"— preço não estava lateralizado o suficiente antes do sweep"
        )
        return resultado

    # ── NOVO: RSI extremo obrigatório — pedido explícito, réplica do
    # processo manual do Juninho: "RSI no talo pra baixo" no suporte
    # (long) ou "RSI no talo pra cima" na resistência (short).
    #
    # IMPORTANTE (achado em teste): o RSI é checado no candle
    # IMEDIATAMENTE ANTERIOR ao sweep, não no próprio candle da
    # rejeição. Motivo: o candle do sweep, por definição, tem um
    # movimento de reversão forte (é exigido por `candle_e_decisivo`) —
    # e esse mesmo movimento já "esfria" o RSI na hora que ele acontece.
    # Testado com dado sintético: RSI chegou a 100 três candles antes da
    # rejeição, e caiu pra 45 NO candle da rejeição — checar no candle
    # errado faria esse filtro quase nunca passar. "RSI no talo" é o
    # estado de ANTES da rejeição, não durante ela.
    #
    # Reaproveita as mesmas constantes e a mesma função
    # (`rsi_extremo_no_candle`) já usadas no modo Antecipado v2, pra
    # manter o critério consistente no projeto todo. ──
    direcao_provisoria = 'alta' if sweep['lado'] == 'baixa' else 'baixa'
    idx_rsi = max(sweep['index'] - 1, 0)
    rsi_val = rsi_extremo_no_candle(exec_candles, idx_rsi)
    if rsi_val is None:
        resultado['motivo'] = f"sweep na {sweep['tipo_zona']} ok, mas RSI indisponível"
        return resultado

    resultado['rsi'] = round(rsi_val, 1)
    rsi_ok = (direcao_provisoria == 'alta' and rsi_val <= RSI_EXTREMO_BAIXA) or \
             (direcao_provisoria == 'baixa' and rsi_val >= RSI_EXTREMO_ALTA)
    if not rsi_ok:
        resultado['motivo'] = (
            f"sweep na {sweep['tipo_zona']} ok, mas RSI não está no talo "
            f"(RSI={round(rsi_val,1)}, precisa <={RSI_EXTREMO_BAIXA} pra long ou >={RSI_EXTREMO_ALTA} pra short)"
        )
        return resultado

    direcao = direcao_provisoria
    entry = preco_atual
    sl = aplicar_buffer_stop(sweep['nivel'], direcao)
    risco = abs(entry - sl)
    tp = entry + risco * 2 if direcao == 'alta' else entry - risco * 2

    resultado.update({
        'sinal': True,
        'direcao': direcao,
        'entry': round(entry, 6),
        'sl': round(sl, 6),
        'tp': round(tp, 6),
        'motivo': 'entrada_confirmada',
    })

    segundos_desde = _segundos_desde_ultimo_alerta(db_file, 'scalp_rapido_signal_state', pair)
    em_cooldown = segundos_desde is not None and segundos_desde < SCALP_RAPIDO_COOLDOWN_SECONDS
    resultado['em_cooldown'] = em_cooldown

    _save_rapido_signal(db_file, pair, exec_tf_label, resultado, alerted=not em_cooldown)

    if send_telegram_fn and not em_cooldown:
        arrow = '📈' if direcao == 'alta' else '📉'
        label = 'LONG' if direcao == 'alta' else 'SHORT'
        zona_nome = 'Resistência de ontem' if sweep['tipo_zona'] == 'resistencia' else 'Suporte de ontem'
        msg = f"⚡ <b>Scalp Rápido — Liquidez Forte — {pair}</b>\n\n"
        msg += f"{arrow} <b>{label}</b> | TF execução: {exec_tf_label}\n"
        msg += f"🧱 Sweep na {zona_nome} (zona diária móvel)\n"
        msg += f"📊 RSI no talo: {resultado['rsi']}\n"
        msg += f"📍 Entrada: {resultado['entry']}\n🛑 Stop (curto): {resultado['sl']}\n✅ TP (RR 2:1): {resultado['tp']}\n\n"
        msg += "<b>Sem CHoCH — entrada rápida no sweep + RSI extremo, stop curto, reentrada permitida em 5min.</b>"
        send_telegram_fn(msg)
    elif em_cooldown:
        restante_min = (SCALP_RAPIDO_COOLDOWN_SECONDS - segundos_desde) // 60
        resultado['motivo'] = f'entrada_confirmada, mas em cooldown ({restante_min}min restantes)'

    return resultado


CASCATA_COOLDOWN_SECONDS = 30 * 60  # 30min — meio-termo entre o Normal (45min) e o Rápido (5min)


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


def process_pair_cascata_smc(db_file, pair, w_candles, d1_candles, h4_candles, h1_candles,
                              exec_candles, exec_tf_label, send_telegram_fn=None):
    """
    NOVO — CASCATA SMC COMPLETA. Pedido explícito do Juninho, desenhado
    junto ao longo da conversa, usando a lógica REAL de estrutura do
    LuxAlgo (código-fonte real, não aproximação) pro cálculo de bias:

    1. Bias de Semanal, D1, H4 e H1 — calculados via
       `compute_lux_structure_bias` (réplica da função leg() +
       swing pivot + cruzamento do LuxAlgo). Só prossegue se os 4
       baterem na MESMA direção — nenhum neutro conta como "alinhado",
       e um único timeframe discordando já descarta o ciclo inteiro.
    2. Sweep na zona diária móvel (`compute_zona_diaria_movel` — mesma
       lógica exata do Pine Script "Zonas Diarias Moveis" do Juninho:
       pavio de cima/baixo do candle D1 anterior) — só conta se a
       direção do sweep bater com o macro já validado no passo 1.
    3. Confirmação = CHoCH OU BOS no TF de execução, o que vier
       PRIMEIRO (a mesma pergunta que o Juninho fez: "ou continua a
       subida ou reverte o preço, a confirmação vem da quebra de
       estrutura, seja ela qual for") — mas só conta se a direção dessa
       quebra também bater com o macro.
    4. Regra de ouro: preço tem que estar em DISCOUNT pra comprar ou em
       PREMIUM pra vender (`compute_premium_discount`) — nunca compra
       no topo, nunca vende no fundo, mesmo com tudo mais alinhado.

    Diferente do Modo 5 (Scalp Rápido), que pode furar esse alinhamento
    de timeframes maiores desde que alinhe com os TFs MENORES (já
    validado antes) — a Cascata exige o alinhamento macro completo,
    sem exceção.

    `w_candles` = candles Semanais. Se vier None (API ainda não busca
    Semanal), o bias semanal vira 'neutro' automaticamente e a cascata
    nunca vai alinhar — funciona, só nunca dispara até isso ser
    resolvido no lado do app.py (busca de candles Semanais).
    """
    resultado = {
        'pair': pair, 'exec_tf': exec_tf_label, 'modo': 'cascata_smc',
        'sinal': False, 'direcao': None, 'entry': None, 'sl': None, 'tp': None,
        'bias_semanal': None, 'bias_d1': None, 'bias_h4': None, 'bias_h1': None,
        'evento_tipo': None, 'motivo': None, 'em_cooldown': False,
    }

    bias_w = compute_lux_structure_bias(w_candles) if w_candles else 'neutro'
    bias_d1 = compute_lux_structure_bias(d1_candles)
    bias_h4 = compute_lux_structure_bias(h4_candles) if h4_candles else 'neutro'
    bias_h1 = compute_lux_structure_bias(h1_candles) if h1_candles else 'neutro'
    resultado.update({
        'bias_semanal': bias_w, 'bias_d1': bias_d1, 'bias_h4': bias_h4, 'bias_h1': bias_h1,
    })

    biases = [bias_w, bias_d1, bias_h4, bias_h1]
    if 'neutro' in biases or len(set(biases)) != 1:
        resultado['motivo'] = (
            f"timeframes maiores não alinhados "
            f"(Semanal={bias_w}, D1={bias_d1}, H4={bias_h4}, H1={bias_h1})"
        )
        return resultado

    direcao_macro = biases[0]

    zona_diaria = compute_zona_diaria_movel(d1_candles)
    if not zona_diaria:
        resultado['motivo'] = 'timeframes alinhados, mas sem candle D1 anterior disponível'
        return resultado

    now = int(time.time())

    # ── NOVO: memória de sweep, mesmo padrão dos Modos 1/2 (achado em
    # teste: sem isso, o sweep "some" assim que os candles de
    # confirmação empurram ele pra fora da janela de 10 candles do
    # detect_sweep_zona_diaria_movel). Tenta achar um sweep FRESCO nos
    # candles recentes; se não achar, reaproveita o salvo do ciclo
    # anterior (se a zona ainda for a mesma e não tiver passado tempo
    # demais sem confirmar CHoCH/BOS). ──
    fresh_sweep = detect_sweep_zona_diaria_movel(exec_candles, zona_diaria)

    # zona "genérica" pra comparação de validade — usa a zona do lado
    # que o sweep fresco indicou, ou (se não achou fresco) qualquer uma
    # das duas zonas salvas — a checagem de validade compara contra o
    # que estiver salvo mesmo.
    zona_para_validar = None
    if fresh_sweep:
        zona_para_validar = (
            zona_diaria['resistencia'] if fresh_sweep['tipo_zona'] == 'resistencia' else zona_diaria['suporte']
        )
    else:
        zona_para_validar = zona_diaria['suporte']  # fallback neutro só pra checar validade do salvo

    saved = _load_saved_state(db_file, pair, table='scalp_cascata_zone_state')
    saved_valido = _sweep_ainda_valido(saved, zona_para_validar, now)

    sweep = None
    if fresh_sweep and (not saved_valido or fresh_sweep['t'] >= saved.get('sweep_ts', 0)):
        sweep = {
            'index': fresh_sweep['index'], 'lado': fresh_sweep['lado'],
            'nivel': fresh_sweep['nivel'], 't': fresh_sweep['t'],
            'tipo_zona': fresh_sweep['tipo_zona'],
        }
    elif saved_valido:
        sweep = {
            't': saved['sweep_ts'], 'nivel': saved['sweep_nivel'], 'lado': saved['sweep_lado'],
            'tipo_zona': 'resistencia' if saved['sweep_lado'] == 'alta' else 'suporte',
            'index': len(exec_candles) - 1,  # aproximação — usado só como referência de tempo pro CHoCH/BOS
        }

    if not sweep:
        resultado['motivo'] = f'timeframes alinhados em {direcao_macro}, mas sem sweep na zona diária ainda'
        # salva a zona mesmo sem sweep, pra manter rastro do ciclo
        _save_zone_state(db_file, pair, {'top': zona_para_validar['top'], 'bottom': zona_para_validar['bottom']},
                          'zona', now, table='scalp_cascata_zone_state')
        return resultado

    # salva o sweep (fresco ou reaproveitado) pro próximo ciclo poder
    # reencontrar mesmo se ele sair da janela de candles recentes
    zona_do_sweep = zona_diaria['resistencia'] if sweep['tipo_zona'] == 'resistencia' else zona_diaria['suporte']
    _save_zone_state(
        db_file, pair, {'top': zona_do_sweep['top'], 'bottom': zona_do_sweep['bottom']},
        'sweep', now, sweep=sweep, table='scalp_cascata_zone_state',
    )

    sweep_direcao = 'alta' if sweep['lado'] == 'baixa' else 'baixa'
    if sweep_direcao != direcao_macro:
        resultado['motivo'] = (
            f"sweep aponta {sweep_direcao}, mas o macro alinhado é {direcao_macro} "
            f"— descartado (sweep contra o bias maior)"
        )
        return resultado

    choch = detect_choch_after_sweep(exec_candles, sweep)
    bos = detect_bos_continuation_after_sweep(exec_candles, sweep)
    evento = None
    evento_tipo = None
    if choch and bos:
        if choch['t'] <= bos['t']:
            evento, evento_tipo = choch, 'CHoCH'
        else:
            evento, evento_tipo = bos, 'BOS'
    elif choch:
        evento, evento_tipo = choch, 'CHoCH'
    elif bos:
        evento, evento_tipo = bos, 'BOS'

    if not evento:
        resultado['motivo'] = f'sweep alinhado com o macro ({direcao_macro}), mas sem CHoCH nem BOS confirmado ainda'
        return resultado

    if evento['direcao'] != direcao_macro:
        resultado['motivo'] = (
            f"{evento_tipo} confirmou {evento['direcao']}, contra o macro {direcao_macro} — descartado"
        )
        return resultado

    resultado['evento_tipo'] = evento_tipo

    preco_atual = exec_candles[-1]['c']
    pd_zone = compute_premium_discount(exec_candles)
    if pd_zone:
        if direcao_macro == 'alta' and preco_atual > pd_zone['equilibrium']:
            resultado['motivo'] = (
                f"{evento_tipo} ok e alinhado, mas preço está em PREMIUM "
                f"({round(preco_atual,6)} > equilíbrio {round(pd_zone['equilibrium'],6)}) — não compra no topo"
            )
            return resultado
        if direcao_macro == 'baixa' and preco_atual < pd_zone['equilibrium']:
            resultado['motivo'] = (
                f"{evento_tipo} ok e alinhado, mas preço está em DISCOUNT "
                f"({round(preco_atual,6)} < equilíbrio {round(pd_zone['equilibrium'],6)}) — não vende no fundo"
            )
            return resultado

    entry = preco_atual
    sl = aplicar_buffer_stop(sweep['nivel'], direcao_macro)
    risco = abs(entry - sl)
    tp = entry + risco * 2 if direcao_macro == 'alta' else entry - risco * 2

    resultado.update({
        'sinal': True,
        'direcao': direcao_macro,
        'entry': round(entry, 6),
        'sl': round(sl, 6),
        'tp': round(tp, 6),
        'motivo': 'entrada_confirmada',
    })

    segundos_desde = _segundos_desde_ultimo_alerta(db_file, 'scalp_cascata_signal_state', pair)
    em_cooldown = segundos_desde is not None and segundos_desde < CASCATA_COOLDOWN_SECONDS
    resultado['em_cooldown'] = em_cooldown

    _save_cascata_signal(db_file, pair, exec_tf_label, resultado, alerted=not em_cooldown)

    if send_telegram_fn and not em_cooldown:
        arrow = '📈' if direcao_macro == 'alta' else '📉'
        label = 'LONG' if direcao_macro == 'alta' else 'SHORT'
        msg = f"🌊 <b>Cascata SMC — {pair}</b>\n\n"
        msg += f"{arrow} <b>{label}</b> | TF execução: {exec_tf_label}\n"
        msg += f"📊 Alinhamento: Semanal={bias_w} | D1={bias_d1} | H4={bias_h4} | H1={bias_h1}\n"
        msg += f"🔨 Confirmação: {evento_tipo}\n"
        msg += f"📍 Entrada: {resultado['entry']}\n🛑 Stop: {resultado['sl']}\n✅ TP (RR 2:1): {resultado['tp']}\n\n"
        msg += "<b>Todos os timeframes maiores alinhados — sweep na zona diária + confirmação de estrutura.</b>"
        send_telegram_fn(msg)
    elif em_cooldown:
        restante_min = (CASCATA_COOLDOWN_SECONDS - segundos_desde) // 60
        resultado['motivo'] = f'entrada_confirmada, mas em cooldown ({restante_min}min restantes)'

    return resultado


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

    contra_alta = direcao == 'alta' and (bias_d1 == 'baixa' and bias_h4 == 'baixa')  # afrouxado: só bloqueia se D1 E H4 concordarem contra
    contra_baixa = direcao == 'baixa' and (bias_d1 == 'alta' and bias_h4 == 'alta')  # afrouxado: só bloqueia se D1 E H4 concordarem contra
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
    sl = aplicar_buffer_stop(sweep['nivel_pavio'], direcao)
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

    # ── Cooldown por tempo (45min), igual ao modo normal — cobre o caso
    # de preço oscilar um pouco (passa no teste de "preço quase
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


# ═══════════════════════════════════════════════════════════════════════
# MODO "CONFLUÊNCIA DE INDICADORES" — pedido explícito: "quero que ele
# fique procurando TODOS os setups de acordo com esses indicadores", não
# só depois que a sequência ICT completa (zona→sweep→CHoCH→entrada) já
# fechou. Esse modo NÃO espera CHoCH nenhum — roda em cima dos mesmos
# indicadores técnicos já calculados (MACD, ADX, EMAs, Stochastic, RSI,
# Bollinger, VWAP, Ichimoku, Monte Carlo, Candle Pattern) e conta quantos
# estão VOTANDO pro mesmo lado. Bate um mínimo de votos concordando =
# sinal, mesmo sem zona D1/sweep/CHoCH confirmados.
#
# É um tipo de setup DIFERENTE dos outros modos — mais rápido de achar,
# mas também mais solto (não exige a estrutura ICT toda). Por isso o
# Stop é calculado via ATR (não via nível de sweep, que esse modo nem
# usa), e o threshold de votos é propositalmente alto (7 de 10) pra não
# virar ruído.
# ═══════════════════════════════════════════════════════════════════════

VOTOS_MINIMOS_SINAL = 7   # de um total de 11 indicadores votantes (agora com Volume Profile POC)
ATR_MULT_STOP = 1.5       # stop = 1.5x ATR de distância da entrada
RR_INDICADORES = 2.0      # TP = 2x o risco, mesmo padrão dos outros modos


def _votos_indicadores(indicadores, preco_atual):
    """
    Cada indicador vota 'alta', 'baixa' ou None (sem opinião clara).
    Retorna (voto_alta, voto_baixa, total_votantes, detalhes[(nome, voto)]).
    Só conta como "votante" quem realmente teve dado suficiente pra opinar
    — indicador sem dado (None) não entra no total, não distorce a conta.
    """
    votos = []

    macd_hist = indicadores.get('macd_hist')
    if macd_hist is not None:
        votos.append(('macd', 'alta' if macd_hist > 0 else 'baixa'))

    ema9, ema21, ema50 = indicadores.get('ema9'), indicadores.get('ema21'), indicadores.get('ema50')
    if ema9 is not None and ema21 is not None and ema50 is not None:
        if ema9 > ema21 > ema50:
            votos.append(('emas', 'alta'))
        elif ema9 < ema21 < ema50:
            votos.append(('emas', 'baixa'))
        # EMAs desalinhadas (sem stack claro) = sem voto, fica de fora

    rsi = indicadores.get('rsi14')
    if rsi is not None:
        if rsi <= 35:
            votos.append(('rsi', 'alta'))   # sobrevendido -> favorece reversão pra cima
        elif rsi >= 65:
            votos.append(('rsi', 'baixa'))  # sobrecomprado -> favorece reversão pra baixo

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
    if vwap is not None:
        votos.append(('vwap', 'alta' if preco_atual > vwap else 'baixa'))

    # ── NOVO: Volume Profile POC — preço ACIMA do nível de maior volume
    # trocado recentemente (POC) indica que compradores já defenderam
    # esse nível como suporte (viés de alta); preço ABAIXO indica que
    # ele age como resistência (viés de baixa). Não é o mesmo voto do
    # VWAP (VWAP é média ponderada por volume da sessão inteira; POC é
    # o preço ESPECÍFICO onde mais volume trocou de mãos) — cada um
    # reforça o outro sem duplicar exatamente a mesma informação. ──
    poc = indicadores.get('volume_profile_poc')
    if poc is not None and poc > 0:
        votos.append(('volume_profile_poc', 'alta' if preco_atual > poc else 'baixa'))

    ichimoku = indicadores.get('ichimoku') or {}
    senkou_a, senkou_b = ichimoku.get('senkou_a'), ichimoku.get('senkou_b')
    if senkou_a is not None and senkou_b is not None:
        topo_nuvem, fundo_nuvem = max(senkou_a, senkou_b), min(senkou_a, senkou_b)
        if preco_atual > topo_nuvem:
            votos.append(('ichimoku', 'alta'))
        elif preco_atual < fundo_nuvem:
            votos.append(('ichimoku', 'baixa'))
        # preço DENTRO da nuvem = indecisão, sem voto

    mc = indicadores.get('monte_carlo') or {}
    prob_alta, prob_baixa = mc.get('prob_alta_pct'), mc.get('prob_baixa_pct')
    if prob_alta is not None and prob_baixa is not None:
        if prob_alta >= 55:
            votos.append(('monte_carlo', 'alta'))
        elif prob_baixa >= 55:
            votos.append(('monte_carlo', 'baixa'))

    padrao = indicadores.get('candle_pattern')
    if padrao in ('Engolfo de Alta', 'Martelo (Hammer)'):
        votos.append(('candle_pattern', 'alta'))
    elif padrao in ('Engolfo de Baixa', 'Estrela Cadente (Shooting Star)'):
        votos.append(('candle_pattern', 'baixa'))

    # ADX não vota direção (só mede força) — entra separado como
    # confirmação de qualidade, não como voto de lado
    adx = indicadores.get('adx14')
    tendencia_forte = adx is not None and adx >= 25

    votos_alta = sum(1 for _, v in votos if v == 'alta')
    votos_baixa = sum(1 for _, v in votos if v == 'baixa')
    return votos_alta, votos_baixa, len(votos), votos, tendencia_forte


def _stop_via_ultimo_swing(exec_candles, direcao, lookback=SWING_LOOKBACK):
    """
    NOVO — stop baseado em ESTRUTURA REAL, não em ATR. Mesmo espírito
    dos outros modos (stop sempre no nível do último sweep): aqui, como
    o Modo Confluência de Indicadores não calcula sweep (não depende de
    zona D1), usa o ÚLTIMO SWING relevante como proxy de estrutura —
    o último fundo (LONG) ou o último topo (SHORT) marcado nos candles
    de execução, que é exatamente onde ficaria o nível de liquidez que
    o preço "não pode" romper sem invalidar a tese.

    - LONG: stop = último swing LOW (abaixo dele, sem suporte)
    - SHORT: stop = último swing HIGH (acima dele, sem resistência)

    Retorna None se não houver swing suficiente ainda (par muito novo/
    poucos candles) — nesse caso o chamador decide o fallback.
    """
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


def process_pair_scalp_indicadores(db_file, pair, exec_candles, exec_tf_label, send_telegram_fn=None):
    """
    Modo independente da sequência ICT — fica procurando setup TODO ciclo,
    baseado só em quantos indicadores técnicos concordam na mesma direção.
    Não precisa de zona D1, sweep nem CHoCH confirmados.

    Regra: precisa de pelo menos VOTOS_MINIMOS_SINAL (7 de ~11) indicadores
    votando pro mesmo lado. Sem isso, sem sinal — não dispara com maioria
    fraca só porque "a maioria" já é alguma coisa.

    Stop SEMPRE via ESTRUTURA REAL (último swing high/low nos candles de
    execução — o proxy mais próximo de "abaixo/acima do último sweep"
    que esse modo tem, já que ele não calcula sweep de verdade). ATR só
    entra como FALLBACK, e só no caso raríssimo de não haver swing
    suficiente ainda (par muito novo) — nesse caso o stop via ATR é
    melhor que travar o modo inteiro, mas fica marcado explicitamente no
    resultado (`stop_via='atr_fallback'`) pra nunca confundir com stop
    estrutural de verdade. TP = RR 2:1 fixo, mesmo padrão dos outros
    modos, calculado em cima do MESMO risco (distância entrada→stop).
    """
    preco_atual = exec_candles[-1]['c']
    indicadores = compute_technical_indicators(exec_candles)

    resultado = {
        'pair': pair, 'exec_tf': exec_tf_label, 'modo': 'confluencia_indicadores',
        'sinal': False, 'direcao': None, 'entry': None, 'sl': None, 'tp': None,
        'stop_via': None,
        'score': 0, 'votos_favor': 0, 'votos_total': 0, 'votos_detalhe': [],
        'tendencia_forte': False, 'indicadores': indicadores, 'em_cooldown': False,
        'motivo': None,
    }

    votos_alta, votos_baixa, total, detalhe_votos, tendencia_forte = _votos_indicadores(indicadores, preco_atual)
    resultado['votos_total'] = total
    resultado['votos_detalhe'] = detalhe_votos
    resultado['tendencia_forte'] = tendencia_forte

    if total == 0:
        resultado['motivo'] = 'dados insuficientes pra votar (par muito novo ou poucos candles)'
        return resultado

    if votos_alta >= VOTOS_MINIMOS_SINAL and votos_alta > votos_baixa:
        direcao = 'alta'
        votos_favor = votos_alta
    elif votos_baixa >= VOTOS_MINIMOS_SINAL and votos_baixa > votos_alta:
        direcao = 'baixa'
        votos_favor = votos_baixa
    else:
        resultado['motivo'] = f'sem maioria suficiente ainda (alta={votos_alta}, baixa={votos_baixa}, mínimo={VOTOS_MINIMOS_SINAL})'
        resultado['votos_favor'] = max(votos_alta, votos_baixa)
        return resultado

    entry = preco_atual

    # ── Stop SEMPRE via estrutura (último swing) — ATR só é usado se não
    # houver swing algum disponível ainda, e nesse caso fica marcado.
    # Aplica o mesmo buffer de segurança dos outros modos (0.1% além do
    # nível), já que o ATR fallback já tem margem própria embutida no
    # multiplicador e não precisa do buffer extra. ──
    sl_bruto = _stop_via_ultimo_swing(exec_candles, direcao)
    stop_via = 'estrutura'
    if sl_bruto is None:
        atr = indicadores.get('atr14')
        if not atr or atr <= 0:
            resultado['motivo'] = 'votos suficientes, mas sem estrutura nem ATR disponível pra calcular stop'
            return resultado
        stop_dist = atr * ATR_MULT_STOP
        sl = entry - stop_dist if direcao == 'alta' else entry + stop_dist
        stop_via = 'atr_fallback'
    else:
        sl = aplicar_buffer_stop(sl_bruto, direcao)

    # ── Validação de sanidade: o swing tem que estar do lado CORRETO do
    # preço atual (fundo abaixo da entrada pra LONG, topo acima pra
    # SHORT) — senão o "último swing" pode ser um remanescente de outro
    # movimento e não faz sentido como stop. Se isso acontecer, cai pro
    # fallback de ATR em vez de usar um nível sem lógica. ──
    swing_invalido = (direcao == 'alta' and sl >= entry) or (direcao == 'baixa' and sl <= entry)
    if swing_invalido:
        atr = indicadores.get('atr14')
        if not atr or atr <= 0:
            resultado['motivo'] = 'último swing do lado errado do preço e ATR indisponível — sem stop confiável'
            return resultado
        stop_dist = atr * ATR_MULT_STOP
        sl = entry - stop_dist if direcao == 'alta' else entry + stop_dist
        stop_via = 'atr_fallback'

    risco = abs(entry - sl)
    tp = entry + risco * RR_INDICADORES if direcao == 'alta' else entry - risco * RR_INDICADORES

    resultado['votos_favor'] = votos_favor
    resultado['score'] = round(100 * votos_favor / total)
    resultado['direcao'] = direcao
    resultado['stop_via'] = stop_via

    resultado.update({
        'sinal': True,
        'entry': round(entry, 6),
        'sl': round(sl, 6),
        'tp': round(tp, 6),
        'motivo': f'{votos_favor}/{total} indicadores concordando em {direcao}' + (' + tendência forte (ADX≥25)' if tendencia_forte else ''),
    })

    # dedup por tempo — mesmo Cooldown de 45min dos outros modos
    segundos_desde = _segundos_desde_ultimo_alerta(db_file, 'scalp_indicadores_signal_state', pair)
    em_cooldown = segundos_desde is not None and segundos_desde < COOLDOWN_SECONDS
    resultado['em_cooldown'] = em_cooldown

    try:
        signal_id = f"ind_{pair}_{int(time.time()*1000)}"
        with sqlite3.connect(db_file) as conn:
            cursor = conn.cursor()
            cursor.execute('''
                INSERT INTO scalp_indicadores_signal_state
                    (id, pair, created_at, exec_tf, direcao, score, votos_favor, votos_total, entry, sl, tp, alerted)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ''', (
                signal_id, pair, int(time.time()), exec_tf_label, direcao, resultado['score'],
                votos_favor, total, resultado['entry'], resultado['sl'], resultado['tp'],
                0 if em_cooldown else 1,
            ))
            conn.commit()
    except Exception as e:
        print(f"[scalp_engine] erro ao salvar signal de indicadores de {pair}: {e}")

    if send_telegram_fn and not em_cooldown:
        arrow = '📈' if direcao == 'alta' else '📉'
        label = 'LONG' if direcao == 'alta' else 'SHORT'
        stop_label = 'Stop (último swing)' if resultado.get('stop_via') == 'estrutura' else 'Stop (1.5x ATR — fallback, sem swing disponível)'
        msg = f"📊 <b>Confluência de Indicadores — {pair}</b>\n\n"
        msg += f"{arrow} <b>{label}</b> | TF execução: {exec_tf_label}\n"
        msg += f"🗳️ {votos_favor}/{total} indicadores concordando"
        if tendencia_forte:
            msg += " | ADX confirma tendência forte"
        msg += "\n"
        msg += f"📍 Entrada: {resultado['entry']}\n🛑 {stop_label}: {resultado['sl']}\n✅ TP (RR 2:1): {resultado['tp']}\n\n"
        msg += "<b>Sem zona D1/sweep/CHoCH — setup baseado só em indicadores técnicos, posição menor recomendada.</b>"
        send_telegram_fn(msg)
    elif em_cooldown:
        restante_min = (COOLDOWN_SECONDS - segundos_desde) // 60
        resultado['motivo'] += f' (em cooldown, {restante_min}min restantes)'

    return resultado
