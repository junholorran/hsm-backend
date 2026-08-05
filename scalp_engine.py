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
#
# ── CORREÇÃO 05/08 (Juninho) ──────────────────────────────────────────
# Zonas D1 do scalp_engine estavam saindo deslocadas em relação às zonas
# reais vistas no TradingView (confirmado em BTCUSDT, AAVEUSDT, SOLUSDT,
# BNBUSDT). Causa raiz: compute_d1_zones() usava janela de pivô (lb) de
# apenas 2 candles — curta demais, qualifica qualquer oscilação de 2
# candles como "pivô estrutural" e mistura ruído com estrutura real no
# cluster. Corrigido pra lb=3, igual ao cascade_engine.py (que nunca
# teve esse problema). Também passou a expor a zona (top/bottom/toques)
# na mensagem "Manipulação detectada" do Telegram, pra dar pra conferir
# a zona usada direto contra o gráfico, não só o nível do sweep.
# ─────────────────────────────────────────────────────────────────────────

import sqlite3
import time
import random
import requests
from datetime import datetime, timezone, timedelta

SCORE_THRESHOLD_SINAL = 75
COOLDOWN_SECONDS = 45 * 60  # 45min — meio-termo da faixa 30-60min do Vortex
TOLERANCIA_CLUSTER_PCT = 0.006   # 0.6% — mesma tolerância usada no cascade pra clusterizar toques
MIN_EVENTOS_BANDA = 2            # mínimo de toques pra uma banda D1 ser considerada válida
MIN_FVG_GAP_PCT = 0.0005         # 0.05% do preço — FVG menor que isso é ruído, não conta como zona de entrada
MIN_CANDLE_BODY_RATIO = 0.35     # candle de confirmação precisa ter corpo >= 35% do range total (afrouxado de 50% — pedido explícito pra disparar mais fácil, depois de ver setup real de score 92 ser bloqueado por esse filtro)
STOP_BUFFER_PCT = 0.001          # 0.1% de folga além do nível estrutural — stop nunca fica colado no pavio exato
D1_LOOKBACK_DIAS = 200           # ── AJUSTADO 05/08 (v2): com o extrator de swing no padrão LuxAlgo (swing_size=50), um swing só se confirma depois de romper uma janela de 50 candles — em 45 dias praticamente não sobrava swing nenhum pra formar zona. 200 dias cobre o mesmo range que o Kairos já busca na Bybit (200 candles D1), sem cortar estrutura que o LuxAlgo ainda mostra no gráfico.
ZONA_FORTE_TOLERANCIA_PCT = 0.0015  # 0.15% — bem mais apertada que a zona D1 (0.6%), pro modo Scalp Rápido
ZONA_FORTE_MIN_TOQUES = 3           # "liquidez forte" de verdade — mais rigoroso que o mínimo de 2 dos outros modos
SCALP_RAPIDO_COOLDOWN_SECONDS = 5 * 60  # 5min — bem mais curto, permite reentrada rápida se tomar stop
ZONA_MOVEL_LOOKBACK = 20                # candles recentes (TF de execução) pra formar a região móvel
ZONA_MOVEL_MAX_LARGURA_PCT = 0.01       # 1% — região precisa estar "apertada" (lateralizada) pra disparar
SWING_LOOKBACK = 5               # candles de cada lado pra confirmar swing high/low no TF de execução
SWEEP_MEMORY_MAX_AGE_SECONDS = 12 * 3600  # sweep salvo expira depois de 12h sem confirmar CHoCH/BOS

# ── NOVO: gate obrigatório de RSI (regra fixa) — aplicado nos modos
# Normal (CHoCH) e Continuação (BOS). Antes, o RSI só somava pontos no
# score (bônus), nunca bloqueava — o que permitia abrir SHORT com RSI
# sobrevendido (contra a direção) ou LONG com RSI sobrecomprado, desde
# que os outros componentes do score já somassem 75+. Isso já causou
# entrada real errada (short no suporte com RSI baixo). Regra fixa:
# RSI >= 60 bloqueia LONG (mercado já esticado pra cima), RSI <= 40
# bloqueia SHORT (mercado já esticado pra baixo). Entre 40-60 não
# bloqueia (zona neutra). ──
RSI_GATE_BLOQUEIA_LONG_ACIMA = 60
RSI_GATE_BLOQUEIA_SHORT_ABAIXO = 40

# ── NOVO 05/08: gates inspirados na estrutura da Vortex Trade IA (GATE_A
# a GATE_F que ela expõe no "Por que este sinal foi gerado?"). Aplicados
# nos modos Normal (CHoCH) e Continuação (BOS) — os dois modos "cheios"
# que já tinham score/RR/indicadores completos. Cada gate é um passa/
# não passa, reportado no resultado (campo 'gates'), igual a Vortex
# reporta GATE_C_MONTE_CARLO, GATE_E_MIN_RR etc. individualmente. ──

# GATE A — Regime de Mercado: só entra se o mercado estiver em
# tendência (ADX >= threshold). Ataca direto o problema visto no SOL —
# mercado lateralizado gerando sinal atrás de sinal que belisca o stop
# sem ir a lugar nenhum. Regime "ranging" bloqueia a entrada.
REGIME_ADX_THRESHOLD = 20
REGIME_GATE_ATIVO = True

# GATE E — R:R mínimo obrigatório (a Vortex reporta "GATE_E_MIN_RR").
# Hoje o Kairos só calculava RR no prompt do Trade Ao Vivo (ICT via
# Claude) — os modos de scalp ao vivo nunca bloqueavam por RR baixo.
MIN_RR_GATE = 1.5
RR_GATE_ATIVO = True

# ── NOVO 05/08: R:R alvo configurável POR MODO — antes fixo em 2.0 em
# todos, a Vortex varia entre 2.0-3.0 dependendo do setup. Cada modo
# recebeu o valor que faz sentido pro seu perfil de risco: Normal e
# Continuação (mais estruturados, zona+sweep+CHoCH/BOS) sobem pra 2.5;
# Cascata SMC (o mais rigoroso, exige 4 timeframes alinhados) vai pro
# valor mais alto, 3.0; Scalp Rápido (mais agressivo, sem CHoCH, stop
# curto) mantém 2.0 — subir o alvo ali só pra distância do TP crescer
# sem crescer a qualidade do gatilho.
RR_TARGET_NORMAL = 2.5
RR_TARGET_CONTINUACAO = 2.5
RR_TARGET_RAPIDO = 2.0
RR_TARGET_CASCATA = 3.0

# GATE C — Monte Carlo como gate, não só bônus de score. Antes só
# somava +6 no compute_score; agora também pode bloquear a entrada se
# a probabilidade não favorecer a direção do sinal.
MONTE_CARLO_GATE_MIN_PROB = 55
MONTE_CARLO_GATE_ATIVO = True


def compute_market_regime(candles, adx_threshold=REGIME_ADX_THRESHOLD):
    """
    Classifica o regime de mercado como 'trending' ou 'ranging', igual
    ao campo "Regime: TRENDING" que a Vortex mostra. Usa ADX(14) —
    ADX >= threshold indica tendência real, abaixo disso é lateralização
    (ranging), onde sweeps/CHoCH tendem a ser ruído, não movimento real.
    Retorna (regime: str, adx_atual: float|None).
    """
    adx_series = compute_adx(candles, 14)
    adx_atual = next((v for v in reversed(adx_series) if v is not None), None)
    if adx_atual is None:
        return 'indefinido', None
    regime = 'trending' if adx_atual >= adx_threshold else 'ranging'
    return regime, round(adx_atual, 2)


def aplicar_gates_entrada(direcao, entry, sl, tp, indicadores, candles_para_regime, incluir_regime=True):
    """
    Roda os gates estilo Vortex (Regime, R:R mínimo, Monte Carlo) numa
    entrada já calculada. Retorna (passou: bool, gates: list de dict
    {'nome':, 'passou':, 'detalhe':}) — cada gate reportado individual,
    igual a Vortex faz no "Filtros de Qualidade".

    `incluir_regime=False` pula o Gate de Regime — usado em modos cujo
    propósito é justamente operar fora de tendência clara (Antecipado
    v2, que caça reversão em RSI extremo, e Scalp Rápido, que existe
    pra ser ágil independente do regime).
    """
    gates = []
    passou_tudo = True

    if REGIME_GATE_ATIVO and incluir_regime:
        regime, adx_val = compute_market_regime(candles_para_regime)
        regime_ok = regime == 'trending'
        gates.append({
            'nome': 'GATE_A_REGIME',
            'passou': regime_ok,
            'detalhe': f"Regime: {regime.upper()} (ADX={adx_val})" if adx_val is not None else "ADX indisponível",
        })
        if not regime_ok:
            passou_tudo = False

    if RR_GATE_ATIVO and entry and sl and tp:
        risco = abs(entry - sl)
        retorno = abs(tp - entry)
        rr = round(retorno / risco, 2) if risco > 0 else 0
        rr_ok = rr >= MIN_RR_GATE
        gates.append({
            'nome': 'GATE_E_MIN_RR',
            'passou': rr_ok,
            'detalhe': f"R:R {rr} {'OK' if rr_ok else f'abaixo do mínimo {MIN_RR_GATE}'}",
        })
        if not rr_ok:
            passou_tudo = False

    if MONTE_CARLO_GATE_ATIVO and indicadores:
        mc = indicadores.get('monte_carlo') or {}
        prob_alta = mc.get('prob_alta_pct')
        prob_baixa = mc.get('prob_baixa_pct')
        if prob_alta is not None and prob_baixa is not None:
            prob_favoravel = prob_alta if direcao == 'alta' else prob_baixa
            mc_ok = prob_favoravel >= MONTE_CARLO_GATE_MIN_PROB
            gates.append({
                'nome': 'GATE_C_MONTE_CARLO',
                'passou': mc_ok,
                'detalhe': f"Prob. favorável {prob_favoravel}% {'OK' if mc_ok else f'abaixo de {MONTE_CARLO_GATE_MIN_PROB}%'}",
            })
            if not mc_ok:
                passou_tudo = False
        else:
            gates.append({'nome': 'GATE_C_MONTE_CARLO', 'passou': True, 'detalhe': 'dados insuficientes — não bloqueia'})

    # ── NOVO 05/08: GATE_B_HORARIO — bloqueia entrada nas janelas de
    # baixa liquidez conhecidas (transição Ásia-Europa, fechamento de
    # NY), inspirado nos "Horários a Evitar" da Vortex. Diferente do
    # bônus de killzone (que soma pontos quando o horário é bom), esse
    # gate ativamente BLOQUEIA quando o horário é ruim. ──
    horario_ruim, nome_janela = esta_em_horario_ruim()
    gates.append({
        'nome': 'GATE_B_HORARIO',
        'passou': not horario_ruim,
        'detalhe': f"Janela de baixa liquidez: {nome_janela}" if horario_ruim else "Horário OK",
    })
    if horario_ruim:
        passou_tudo = False

    return passou_tudo, gates


NOMES_PADRAO_CANDLE_PT = {
    'Engolfo de Alta': 'Engolfo (Engulfing) — domínio comprador com momentum forte',
    'Engolfo de Baixa': 'Engolfo (Engulfing) — domínio vendedor com momentum forte',
    'Martelo (Hammer)': 'Martelo — rejeição de fundo com pavio longo',
    'Estrela Cadente (Shooting Star)': 'Estrela Cadente — rejeição de topo com pavio longo',
    'Doji': 'Doji — indecisão, sem domínio claro',
}


def _formatar_motivos_principais(regime, adx_val, evento_tipo, evento_direcao, entry_zone_tipo,
                                  score, gates, candle_pattern=None, na_killzone=False, killzone_nome=None):
    """
    Monta o bloco "Motivos Principais" no mesmo espírito do card "Por
    que este sinal foi gerado?" da Vortex (Regime/Estrutura/Gatilho/
    Tipo/Score), mas em cima de dados que o Kairos já calcula de
    verdade — nada decorativo, cada linha é rastreável até uma função
    real do motor.
    """
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


# ── NOVO 05/08: itens adicionais catalogados do "Relatório Executivo
# Completo" da Vortex — cada um só entrou aqui porque é rastreável até
# um cálculo real, não decoração. ──

def classificar_qualidade_rr(rr):
    """
    Label textual de qualidade do R:R, igual ao "BAIXA. Relação de
    1:1.0 exige alta taxa de acerto." da Vortex. Isso é INFORMATIVO —
    roda independente do GATE_E_MIN_RR (que já bloqueia sinais com RR
    abaixo do mínimo); aqui é só pra dar contexto de quão folgado é o
    RR de um sinal que já passou no gate.
    """
    if rr is None:
        return None
    if rr < 1.5:
        return f"BAIXA — Relação de 1:{rr} exige taxa de acerto alta (>{round(100/(1+rr))}%) pra ser lucrativo no longo prazo."
    elif rr < 2.5:
        return f"MODERADA — Relação de 1:{rr} é equilibrada, taxa de acerto de ~{round(100/(1+rr))}% já cobre o breakeven."
    else:
        return f"ALTA — Relação de 1:{rr} permite ser lucrativo mesmo com taxa de acerto abaixo de {round(100/(1+rr))}%."


# ── NOVO 05/08: capital configurável — pra o position sizing entrar
# nas mensagens automáticas, precisa saber o capital do usuário. Se
# ficar None (padrão), o bloco de position sizing simplesmente não
# aparece nas mensagens — nunca inventa um valor. Pra ativar, defina
# CAPITAL_USUARIO_USD com seu capital real de trading (não a banca
# toda, só a fatia destinada a essas operações).
CAPITAL_USUARIO_USD = None  # ex: 10000 — mude aqui pro position sizing aparecer nos sinais

# Position sizing — perfis fixos de risco por trade, igual aos 3
# botões (Conservador/Moderado/Agressivo) que a Vortex mostra.
PERFIS_RISCO = {
    'conservador': 0.0075,  # 0.75% do capital por trade (meio da faixa 0.5-1%)
    'moderado': 0.015,      # 1.5% do capital por trade (meio da faixa 1-2%)
    'agressivo': 0.025,     # 2.5% do capital por trade (meio da faixa 2-3%)
}


def calcular_position_sizing(capital, entry, sl, perfil='moderado'):
    """
    Calcula o tamanho de posição sugerido dado o capital do usuário,
    entry/sl do sinal, e o perfil de risco escolhido — igual à
    calculadora "Seu Capital (Banca)" da Vortex. Retorna dict com
    valor em risco (USD) e % do capital arriscado por unidade.
    """
    if not capital or not entry or not sl or entry == sl:
        return None
    risco_pct = PERFIS_RISCO.get(perfil, PERFIS_RISCO['moderado'])
    valor_risco_usd = round(capital * risco_pct, 2)
    distancia_stop = abs(entry - sl)
    # quantidade do ativo que, se bater o stop, perde exatamente valor_risco_usd
    quantidade_sugerida = round(valor_risco_usd / distancia_stop, 6) if distancia_stop > 0 else None
    return {
        'perfil': perfil,
        'risco_pct': round(risco_pct * 100, 2),
        'valor_em_risco_usd': valor_risco_usd,
        'quantidade_sugerida': quantidade_sugerida,
    }


# ── Weak High / Strong Low — classificação de swings por qualidade de
# liquidez (ICT real). Um swing é "Strong" quando foi respeitado por
# múltiplos toques sem ser varrido — indica liquidez ainda intacta e
# reação forte. É "Weak" quando já foi varrido/testado e rompido —
# indica que a liquidez ali já foi capturada, menos confiável como
# alvo. O Kairos já detecta EQH/EQL com toques; essa função só
# classifica o swing mais recente de cada lado. ──

def classificar_forca_swing(swing_nivel, swing_tipo, candles, tolerancia_pct=0.001):
    """
    swing_tipo: 'high' ou 'low'. Retorna 'strong' se o nível nunca foi
    rompido pelos candles seguintes (ainda intacto — liquidez viva),
    'weak' se já foi rompido em algum candle posterior (liquidez já
    capturada).
    """
    if swing_nivel is None or not candles:
        return None
    for c in candles:
        if swing_tipo == 'high' and c['h'] > swing_nivel * (1 + tolerancia_pct):
            return 'weak'
        if swing_tipo == 'low' and c['l'] < swing_nivel * (1 - tolerancia_pct):
            return 'weak'
    return 'strong'


# ── Gate de horário ruim — a Vortex lista janelas explícitas a
# EVITAR (transição Ásia-Europa e fechamento de NY), não só killzones
# boas. Baixa liquidez nessas janelas tende a gerar fakeout/ruído.
# Horários em UTC. ──
HORARIOS_RUINS_UTC = [
    {'nome': 'Transição Ásia-Europa', 'inicio_h': 5, 'fim_h': 7},
    {'nome': 'Fechamento de NY', 'inicio_h': 21, 'fim_h': 23},
]


def esta_em_horario_ruim():
    """Retorna (True, nome_da_janela) se o horário UTC atual cair
    numa janela de baixa liquidez conhecida, senão (False, None)."""
    import datetime
    hora_utc = datetime.datetime.utcnow().hour
    for janela in HORARIOS_RUINS_UTC:
        if janela['inicio_h'] <= hora_utc < janela['fim_h']:
            return True, janela['nome']
    return False, None


def compute_sazonalidade_mensal(db_file, pair, meses_historico=24):
    """
    Sazonalidade real, calculada em cima do próprio histórico de
    sinais resolvidos (win/loss) do Kairos — não é dado de terceiro,
    é o retrospecto real do par nesse mês específico ao longo dos
    últimos meses. Precisa das colunas resultado_final (já
    adicionadas via ALTER TABLE em init_scalp_db).
    """
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


# ── NOVO 05/08: Fear & Greed Index — dado externo real (API pública
# alternative.me, a mesma que a maioria das plataformas usa), com
# cache em memória de 1h pra não bater na API toda vez que um dos 6
# modos rodar um ciclo pra cada par. É crypto-wide, não por par, então
# faz sentido cachear globalmente. ──
_FEAR_GREED_CACHE = {'valor': None, 'classificacao': None, 'timestamp': 0}
FEAR_GREED_CACHE_TTL = 3600  # 1 hora


def get_fear_greed_index():
    """
    Retorna dict {'valor': int 0-100, 'classificacao': str} ou None se
    a API falhar. Cacheado por 1h — não é crítico ter valor exato ao
    segundo, o índice muda devagar.
    """
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
    """
    Bloco único reaproveitado pelos 6 modos — monta em texto os itens
    "Relatório Executivo" que fazem sentido dentro de uma mensagem de
    sinal automático: qualidade do R:R, força do swing varrido
    (Strong/Weak), status de mitigação do OB (se a zona de entrada for
    OB), e sazonalidade real do par nesse mês. Cada linha só entra se
    o dado existir — não força nada decorativo.

    (Position sizing fica de fora daqui de propósito: depende do
    capital do usuário, que o Kairos não guarda automaticamente — é
    função separada, `calcular_position_sizing`, pra usar sob demanda.)
    """
    linhas = []

    # R:R quality
    if entry and sl and tp:
        risco = abs(entry - sl)
        retorno = abs(tp - entry)
        if risco > 0:
            rr = round(retorno / risco, 2)
            qualidade = classificar_qualidade_rr(rr)
            if qualidade:
                linhas.append(f"📐 {qualidade}")

    # Força do swing varrido (Strong = liquidez ainda intacta antes do sweep, Weak = já rompida antes)
    if sweep_nivel is not None and sweep_tipo is not None and exec_candles:
        swing_tipo_liquidez = 'high' if sweep_tipo == 'alta' else 'low'
        forca = classificar_forca_swing(sweep_nivel, swing_tipo_liquidez, exec_candles)
        if forca:
            linhas.append(f"💧 Liquidez varrida: {'Strong (intacta até agora)' if forca=='strong' else 'Weak (já tinha sido testada antes)'}")

    # Mitigação do OB, só se a zona de entrada for um Order Block
    if entry_zone_tipo and 'OB' in entry_zone_tipo and obs_com_mitigacao:
        ob_correspondente = next(
            (ob for ob in obs_com_mitigacao if ob.get('bottom') is not None and ob.get('top') is not None
             and ob['bottom'] <= (entry or 0) <= ob['top']), None
        )
        if ob_correspondente is not None:
            linhas.append(f"⚠️ Order Block {'já mitigado antes' if ob_correspondente['mitigado'] else 'ainda intacto (primeira vez)'}")

    # Sazonalidade real
    try:
        saz = compute_sazonalidade_mensal(db_file, pair)
        if saz and saz.get('amostras', 0) > 0:
            linhas.append(f"📅 Sazonalidade real ({pair}, esse mês): {saz['win_rate_pct']}% de acerto em {saz['amostras']} sinais resolvidos")
    except Exception:
        pass

    # Fear & Greed Index (crypto-wide, cacheado 1h)
    try:
        fg = get_fear_greed_index()
        if fg:
            linhas.append(f"🌡️ Fear & Greed Index: {fg['valor']}/100 ({fg['classificacao']})")
    except Exception:
        pass

    # Position sizing — só aparece se CAPITAL_USUARIO_USD estiver
    # configurado (nunca inventa um capital)
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


# Portugal (Europe/Lisbon) — horário local aproximado, sem lib de timezone externa.
# UTC+0 no inverno, UTC+1 no verão (DST). Pra simplificar e evitar dependência
# externa, usamos UTC+1 fixo (cobre a maior parte do ano de trading ativo);
# a diferença de 1h no inverno é aceitável pra um gate informativo de killzone.
PT_UTC_OFFSET_HOURS = 1

KILLZONES = [
    {'nome': 'Ásia', 'inicio_h': 0, 'fim_h': 3},  # ── NOVO: Tokyo/Ásia killzone (ICT), pedido pra bater com a Vortex que tem London/NY/Ásia
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
        # ── NOVO 05/08: colunas de Break Even + Parcial automatizados
        # (estilo Vortex — "BE +6.0 pips" / "PARCIAL +6.0 pips"). Cada
        # tabela de sinal com entry/sl/tp ganha 3 colunas: be_movido
        # (stop já foi avisado pra mover pra entrada), parcial_feita
        # (já avisou realização parcial), e status_gestao (texto livre
        # pra exibir, tipo "BE" ou "PARCIAL +X pips"). Tudo via
        # ALTER TABLE com try/except — se a coluna já existe, ignora. ──
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


def is_in_killzone(now_utc=None):
    """Retorna (bool, nome_killzone_ou_None). Horário Portugal, gate de scalp."""
    now_utc = now_utc or datetime.now(timezone.utc)
    pt_hour = (now_utc + timedelta(hours=PT_UTC_OFFSET_HOURS)).hour
    for kz in KILLZONES:
        if kz['inicio_h'] <= pt_hour < kz['fim_h']:
            return True, kz['nome']
    return False, None


# ── CORRIGIDO 05/08 (v2): extrator de swing points que replica o
# algoritmo REAL do LuxAlgo Smart Money Concepts (não um pivô simétrico
# simples). Confirmado com o Juninho: "Swing Points Length" = 50 nas
# configs do indicador no TradingView. O LuxAlgo usa troca de "perna"
# (leg) — quando o preço rompe o topo/fundo de uma janela de N candles,
# a perna vira, e o candle que ficou exatamente N candles atrás daquela
# virada é marcado como o swing point. É o mesmo método já usado em
# compute_lux_structure_bias() pra calcular viés — aqui reaproveitamos
# a mesma lógica, mas extraindo os NÍVEIS de swing, não só a direção,
# pra alimentar o cluster de zonas D1/H4. Se o Juninho mudar o "Swing
# Points Length" no LuxAlgo, o swing_size aqui tem que mudar junto. ──
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


# ── NOVO 05/08 (v3): Order Blocks D1 no padrão ICT/LuxAlgo — confirmado
# com o Juninho que as caixas coloridas que ele vê no gráfico (a
# referência real) são Order Blocks do LuxAlgo, não swing high/low.
# Conceito ICT: um Order Block é a ÚLTIMA vela CONTRÁRIA ao movimento,
# logo antes de um rompimento de estrutura (BOS) que confirma que
# aquela vela realmente tinha ordens institucionais por trás. Ex: a
# última vela vermelha antes de um impulso de alta que rompe o topo
# anterior = Order Block de alta (zona de demanda). Usa o mesmo
# leg-detection do LuxAlgo (swing_size=50) pra achar os rompimentos de
# estrutura, e filtra por ATR (igual ao "Order Block Filter: Atr" nas
# configs do indicador) pra descartar velas insignificantes que não
# representam movimento institucional real. ──
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

        # ── BOS de alta: fechamento rompe o último swing high → procura
        # a última vela vermelha (bearish) antes desse rompimento ──
        if swing_high_level is not None and not swing_high_crossed and c['c'] > swing_high_level:
            swing_high_crossed = True
            for k in range(i, max(0, i - swing_size), -1):
                cand = d1_candles[k]
                if cand['c'] < cand['o']:  # vela vermelha = candidata a OB de alta
                    rng = cand['h'] - cand['l']
                    if atr_val and rng >= atr_val * atr_mult:
                        obs.append({
                            'top': cand['h'], 'bottom': cand['l'],
                            'tipo': 'demanda', 't': cand['t'],
                        })
                    break

        # ── BOS de baixa: fechamento rompe o último swing low → procura
        # a última vela verde (bullish) antes desse rompimento ──
        if swing_low_level is not None and not swing_low_crossed and c['c'] < swing_low_level:
            swing_low_crossed = True
            for k in range(i, max(0, i - swing_size), -1):
                cand = d1_candles[k]
                if cand['c'] > cand['o']:  # vela verde = candidata a OB de baixa
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

    # dedup — mesma vela pode ter sido pega em rompimentos próximos
    vistos = set()
    unicos = []
    for o in obs:
        chave = (o['t'], o['tipo'])
        if chave not in vistos:
            vistos.add(chave)
            unicos.append(o)

    return unicos


# ─── ZONA D1 — algoritmo REAL do indicador "SRchannel" (Support
# Resistance Channels), autor LonesomeTheBlue, código Pine v6 fornecido
# pelo Juninho. Tradução fiel, linha por linha, do script original —
# não é mais aproximação. Parâmetros confirmados: Pivot Period=10,
# Maximum Channel Width%=5, Minimum Strength=1, Maximum Number of S/R=6,
# Loopback Period=290.
#
# Passo a passo do algoritmo original:
#   1. Pivôs de topo/fundo via ta.pivothigh/pivotlow (janela simétrica
#      de `pivot_period` candles pra cada lado, precisa ser
#      estritamente maior/menor que os dois lados).
#   2. Só ficam pivôs dentro dos últimos `loopback` candles (relativo
#      ao candle mais recente).
#   3. Largura máxima do canal (`cwidth`) = (maior high - menor low)
#      dos últimos 300 candles (FIXO em 300, não é o loopback) vezes
#      `channel_width_pct` / 100.
#   4. Pra CADA pivô (usado como semente), varre TODOS os pivôs (na
#      ordem do mais recente pro mais antigo) e expande uma faixa
#      [lo,hi] incluindo cada pivô cuja inclusão mantenha a largura
#      final dentro de `cwidth`. Cada pivô incluído soma 20 à força
#      bruta desse candidato.
#   5. Em cima da força bruta, soma quantos candles (high OU low) dos
#      últimos `loopback` candles tocam dentro da faixa [lo,hi] desse
#      candidato — isso pesa muito mais canais realmente respeitados
#      pelo preço, não só canais com muitos pivôs.
#   6. Seleção gulosa: pega o candidato de maior força (força mínima =
#      `min_strength` * 20), remove/marca como usado qualquer outro
#      candidato cuja faixa se sobreponha à faixa escolhida, repete até
#      `max_number_sr` canais ou não sobrar candidato válido.
#   7. Classificação suporte/resistência é DINÂMICA, relativa ao preço
#      atual: canal inteiro acima do preço = resistência (oferta);
#      canal inteiro abaixo = suporte (demanda); preço dentro do canal
#      = "mista" (cor neutra no indicador original).
# ─────────────────────────────────────────────────────────────────────
SR_CHANNEL_PIVOT_PERIOD = 10
SR_CHANNEL_MAX_WIDTH_PCT = 5
SR_CHANNEL_MIN_STRENGTH = 1
SR_CHANNEL_MAX_NUMBER = 6
SR_CHANNEL_LOOKBACK_PERIOD = 290
SR_CHANNEL_WIDTH_BASIS_BARS = 300  # fixo no script original, não é o loopback


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

    # ── passo 1+2: pivôs, mais recentes primeiro (equivalente ao
    # array.unshift do Pine — a ORDEM importa pro passo 4), só dentro
    # do loopback ──
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

    # ── passo 3: largura máxima em valor absoluto, base fixa de 300
    # candles (não o loopback) ──
    janela_300 = d1_candles[-SR_CHANNEL_WIDTH_BASIS_BARS:] if n >= SR_CHANNEL_WIDTH_BASIS_BARS else d1_candles
    prdhighest = max(c['h'] for c in janela_300)
    prdlowest = min(c['l'] for c in janela_300)
    cwidth = (prdhighest - prdlowest) * channel_width_pct / 100.0
    if cwidth <= 0:
        return []

    # ── passo 4: get_sr_vals — pra cada pivô-semente, expande a faixa
    # incluindo todo pivô que caiba na largura máxima ──
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

    # ── passo 5: soma toques reais (high/low de candles dentro do
    # loopback tocando na faixa) ──
    start_idx = max(0, last_idx - loopback)
    for cand in candidatos:
        h_, l_ = cand['hi'], cand['lo']
        toques = 0
        for k in range(start_idx, last_idx + 1):
            hk, lk = highs[k], lows[k]
            if (l_ <= hk <= h_) or (l_ <= lk <= h_):
                toques += 1
        cand['forca'] += toques

    # ── passo 6: seleção gulosa, sem sobreposição ──
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

    # ── passo 7: classificação dinâmica relativa ao preço atual ──
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


# ─── ZONA D1 (SRchannel — indicador real usado pelo Juninho no TV) ─────
def compute_d1_zones(d1_candles, lookback_dias=None, swing_size=50):
    # ── SUBSTITUÍDO 05/08 (v5): tradução fiel do código Pine real do
    # SRchannel (author LonesomeTheBlue), fornecido pelo Juninho — não
    # é mais aproximação. lookback_dias/swing_size ficam nos argumentos
    # só por compatibilidade com quem já chama essa função — não são
    # mais usados aqui. ──
    return compute_sr_channels(d1_candles)


# ─── ZONA D1 antiga (cluster de swing S/R) — mantida à parte, não é mais
# usada por padrão pelos 5 modos, mas fica disponível se precisar comparar
# ou voltar atrás. ──
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
            # ── NOVO — classifica a banda como DEMANDA (maioria dos
            # toques foi 'low', preço bateu e subiu = comprador
            # defendendo) ou OFERTA (maioria 'high', preço bateu e
            # caiu = vendedor defendendo). 'mista' quando empatado —
            # usado pro Modo Confluência de Indicadores não vender
            # numa zona de demanda nem comprar numa zona de oferta,
            # já que esse modo não depende de zona D1 por padrão. ──
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
    """
    Mesma detecção do find_order_blocks, mas cada OB ganha o campo
    'mitigado': True se o preço já retornou e tocou dentro da zona
    depois que o OB se formou (igual ao "⚠️ Já mitigado" que a Vortex
    mostra) — indica que a zona já foi "consumida" e tem menos força
    de reação da próxima vez.
    """
    candles = exec_candles[-lookback:] if len(exec_candles) > lookback else exec_candles
    obs = find_order_blocks(exec_candles, lookback)
    for ob in obs:
        idx = ob.pop('idx', None)
        if idx is None:
            ob['mitigado'] = None
            continue
        candles_depois = candles[idx + 2:]  # pula o próprio candle e o de confirmação
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
        # ── NOVO 05/08: zonas SRchannel (o indicador real que o Juninho usa
        # no TV — Pivot Period 10, Largura Máx 5%, Força Mín 1, Máx 6 zonas,
        # Lookback 290) — é isso que os 5 modos (Normal/Continuação/
        # Antecipado/Confluência/Scalp Rápido) usam como "zona D1" agora. ──
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


# ── NOVO 05/08: Micro BOS — confirmação extra de precisão, inspirada
# no gatilho "Micro BOS (Break of Structure)" que a Vortex mostra:
# "Rompimento de estrutura recente (últimas 3 velas) confirmando
# mudança de direção no micro timeframe". Diferente do BOS "grande"
# (que rompe o nível do sweep, podendo levar várias velas), o Micro
# BOS olha só as últimas N velas do TF de execução — serve como
# confirmação adicional de que o movimento tá acontecendo *agora*,
# não é um evento antigo ainda "válido" tecnicamente. ──
MICRO_BOS_LOOKBACK = 3


def detect_micro_bos(exec_candles, direcao, lookback=MICRO_BOS_LOOKBACK):
    """
    Verifica se, nas últimas `lookback` velas do TF de execução, o
    preço rompeu o topo/fundo local imediatamente anterior a essa
    janela — ou seja, se a mudança de direção é recente de verdade,
    não uma quebra de estrutura "velha" que já perdeu força.

    direcao: 'alta' (rompeu topo local pra cima) ou 'baixa' (rompeu
    fundo local pra baixo).

    Retorna dict {'confirmado': bool, 'nivel_rompido': float|None} —
    nunca bloqueia nada sozinho, é só uma confirmação extra pra
    reportar/reforçar, igual a Vortex trata como "Gatilho SMC", não
    como filtro obrigatório adicional.
    """
    if not exec_candles or len(exec_candles) < lookback + 2:
        return {'confirmado': False, 'nivel_rompido': None}

    janela_recente = exec_candles[-lookback:]
    candles_antes = exec_candles[:-lookback]
    if not candles_antes:
        return {'confirmado': False, 'nivel_rompido': None}

    # topo/fundo local imediatamente antes da janela das últimas N velas
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


# ── NOVO 05/08: entrada no melhor preço da zona, pra bater com o
# jeito real de operar do Juninho — ele sempre deixa ORDEM PENDENTE
# (limite), não entra a mercado. Faz sentido colocar a ordem na ponta
# mais vantajosa da zona de entrada (FVG/OB/iFVG/Breaker), não no
# preço aleatório de quando o ciclo rodou:
# - LONG: pede a compra na borda de BAIXO da zona (mais barato possível)
# - SHORT: pede a venda na borda de CIMA da zona (mais caro possível)
# Se o preço nunca voltar até essa borda, a ordem pendente simplesmente
# não enche — isso é o comportamento correto de ordem limite, não um
# bug. É diferente de entrar a mercado no preço que passava na hora.
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


# ── NOVO 05/08: buffer de stop via ATR — pendência antiga. O buffer
# fixo de 0,1% ficava colado demais no nível exato do sweep; em
# mercado lateralizado (como o SOL que gerou os stops seguidos), um
# reteste normal já bastava pra bater o stop mesmo quando a tese do
# trade continuava certa. Buffer via ATR se adapta à volatilidade real
# do momento em vez de ser sempre o mesmo percentual fixo. ──
ATR_BUFFER_MULT = 0.25  # 0.25x o ATR(14) do TF de execução como folga


def aplicar_buffer_stop_atr(nivel, direcao, exec_candles, atr_mult=ATR_BUFFER_MULT, fallback_pct=STOP_BUFFER_PCT):
    """
    Versão do buffer de stop que usa ATR em vez de percentual fixo.
    Se não conseguir calcular ATR (poucos candles), cai no buffer
    percentual fixo antigo como fallback — nunca quebra.
    """
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

    vols = [c.get('v', 0) for c in exec_candles[-20:]]
    if vols:
        media_vol = sum(vols) / len(vols)
        choch_candle = exec_candles[choch['index']] if choch['index'] < len(exec_candles) else None
        if choch_candle and choch_candle.get('v', 0) > media_vol * 1.3:
            score += 8
            detalhes.append(('volume_choch_forte', 8))

    if indicadores is None:
        indicadores = compute_technical_indicators(exec_candles)
    direcao = choch['direcao']

    macd_hist = indicadores.get('macd_hist')
    if macd_hist is not None:
        if (direcao == 'alta' and macd_hist > 0) or (direcao == 'baixa' and macd_hist < 0):
            score += 8
            detalhes.append(('macd_confirmando', 8))

    adx = indicadores.get('adx14')
    if adx is not None and adx >= 25:
        score += 7
        detalhes.append(('adx_tendencia_forte', 7))

    ema9, ema21, ema50 = indicadores.get('ema9'), indicadores.get('ema21'), indicadores.get('ema50')
    if ema9 is not None and ema21 is not None and ema50 is not None:
        if direcao == 'alta' and ema9 > ema21 > ema50:
            score += 8
            detalhes.append(('emas_alinhadas', 8))
        elif direcao == 'baixa' and ema9 < ema21 < ema50:
            score += 8
            detalhes.append(('emas_alinhadas', 8))

    stoch_k, stoch_d = indicadores.get('stoch_k'), indicadores.get('stoch_d')
    if stoch_k is not None and stoch_d is not None:
        if direcao == 'alta' and stoch_k <= 30 and stoch_d <= 30:
            score += 6
            detalhes.append(('stochastic_favoravel', 6))
        elif direcao == 'baixa' and stoch_k >= 70 and stoch_d >= 70:
            score += 6
            detalhes.append(('stochastic_favoravel', 6))

    bb_lower, bb_upper = indicadores.get('bollinger_lower'), indicadores.get('bollinger_upper')
    if bb_lower is not None and bb_upper is not None and sweep.get('nivel') is not None:
        if direcao == 'alta' and sweep['nivel'] <= bb_lower:
            score += 5
            detalhes.append(('bollinger_extremo_favoravel', 5))
        elif direcao == 'baixa' and sweep['nivel'] >= bb_upper:
            score += 5
            detalhes.append(('bollinger_extremo_favoravel', 5))

    atr = indicadores.get('atr14')
    if atr and atr > 0 and sweep.get('nivel') is not None:
        preco_atual_est = exec_candles[-1]['c']
        risco_est = abs(preco_atual_est - sweep['nivel'])
        razao_atr = risco_est / atr
        if 0.5 <= razao_atr <= 4:
            score += 5
            detalhes.append(('atr_risco_saudavel', 5))

    preco_atual = exec_candles[-1]['c']

    vwap = indicadores.get('vwap')
    if vwap is not None:
        if direcao == 'alta' and preco_atual > vwap:
            score += 5
            detalhes.append(('vwap_alinhado', 5))
        elif direcao == 'baixa' and preco_atual < vwap:
            score += 5
            detalhes.append(('vwap_alinhado', 5))

    poc = indicadores.get('volume_profile_poc')
    if poc is not None and poc > 0:
        distancia_pct = abs(preco_atual - poc) / poc
        if distancia_pct <= 0.008:
            score += 5
            detalhes.append(('poc_proximo', 5))

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
        'rsi14': None,
        'entry_zone_top': None,
        'entry_zone_bottom': None,
        'entry_zone_tipo': None,
        'indicadores': None,
        'gates': [],
        'em_cooldown': False,
    }

    try:
        resultado['indicadores'] = compute_technical_indicators(exec_candles)
    except Exception as e:
        print(f"[scalp_engine] erro ao calcular indicadores de {pair}: {e}")

    if not zona:
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

    saved = _load_saved_state(db_file, pair)

    # ── NOVO: alerta de POSSÍVEL ACUMULAÇÃO — dispara UMA vez, quando
    # essa zona D1 específica é vista pela primeira vez (compara com a
    # zona salva do ciclo anterior; mesma zona nos ciclos seguintes não
    # alerta de novo). Etapa 1 da sequência Acumulação → Manipulação →
    # Entrada pedida pelo Juninho — só pro modo Normal/Continuação, não
    # mistura com os outros 4 modos. ──
    zona_e_nova = (
        not saved or saved.get('zona_top') is None
        or abs((saved.get('zona_top') or 0) - zona['top']) > (zona['top'] - zona['bottom'])
        or abs((saved.get('zona_bottom') or 0) - zona['bottom']) > (zona['top'] - zona['bottom'])
    )
    if zona_e_nova and send_telegram_fn:
        msg = f"🟨 <b>Possível Acumulação — {pair}</b>\n\n"
        msg += f"Zona D1 identificada: {round(zona['bottom'],6)} – {round(zona['top'],6)} ({zona['toques']} toques)\n"
        msg += f"👁️ Observando — aguardando manipulação (sweep) pra confirmar."
        send_telegram_fn(msg)

    fresh_sweep = detect_sweep_in_zone(exec_candles, zona)

    # ── NOVO — pedido explícito do Juninho: "não é a borda, é a
    # liquidez". A borda da banda D1 sozinha pode disparar mesmo sem
    # ter um fundo/topo antigo real ali. Agora, todo sweep FRESCO
    # também precisa varrer uma liquidez real e específica (mesma
    # função já usada no Antecipado v2: find_liquidez_antiga) — não só
    # tocar a média da banda. Se não achar liquidez real por trás do
    # sweep, não conta como manipulação de verdade — descarta e segue
    # esperando. ──
    if fresh_sweep:
        tipo_liq = 'high' if fresh_sweep['lado'] == 'alta' else 'low'
        liq_valor, _liq_idx = find_liquidez_antiga(exec_candles, fresh_sweep['index'], tipo_liq)
        if liq_valor is None:
            fresh_sweep = None

    saved_valido = _sweep_ainda_valido(saved, zona, now)

    sweep = None
    if fresh_sweep and (not saved_valido or fresh_sweep['t'] >= saved['sweep_ts']):
        sweep = fresh_sweep
    elif saved_valido:
        sweep = {'t': saved['sweep_ts'], 'nivel': saved['sweep_nivel'], 'lado': saved['sweep_lado']}

    if not sweep:
        resultado['motivo'] = 'sem sweep de liquidez real detectado ainda'
        _save_zone_state(db_file, pair, zona, 'zona', now)
        return resultado

    resultado['sweep_nivel'] = round(sweep['nivel'], 6)
    resultado['sweep_lado'] = sweep['lado']

    # ── NOVO: alerta de MANIPULAÇÃO — dispara UMA vez, na primeira vez
    # que esse sweep específico é detectado (compara com o sweep_ts
    # salvo do ciclo anterior; sweep repetido no mesmo nível não alerta
    # de novo). Cobre o pedido do Juninho de acompanhar a sequência
    # Acumulação → Manipulação → Distribuição em tempo real, com o Viés
    # Diário (D1) junto pra dar contexto (nunca compra resistência,
    # nunca vende suporte — o próprio sweep já respeita isso: sweep no
    # fundo só pode virar LONG, sweep no topo só pode virar SHORT).
    # ── CORREÇÃO 05/08: agora mostra a ZONA usada (top/bottom/toques),
    # não só o nível do sweep — sem isso não dá pra conferir a zona
    # contra o gráfico. ──
    sweep_e_novo = not saved or saved.get('sweep_ts') != sweep.get('t')
    if sweep_e_novo and send_telegram_fn:
        bias_d1_msg = compute_bias_from_swings(d1_candles)
        lado_txt = 'topo (resistência)' if sweep['lado'] == 'alta' else 'fundo (suporte)'

        # ── NOVO: EXPECTATIVA baseada em ICT — não é previsão garantida,
        # é uma inclinação: se o sweep varreu CONTRA o viés diário, o
        # viés maior tende a "vencer" e puxar reversão; se varreu A FAVOR
        # do viés diário, tende a reforçar e continuar. Viés neutro não
        # inclina pra lado nenhum.
        if bias_d1_msg == 'neutro':
            expectativa_txt = "imprevisível (viés diário neutro, sem inclinação clara)"
        elif sweep['lado'] == 'baixa':  # varreu suporte
            expectativa_txt = "provável REVERSÃO (alta)" if bias_d1_msg == 'alta' else "provável CONTINUAÇÃO (baixa)"
        else:  # sweep['lado'] == 'alta', varreu resistência
            expectativa_txt = "provável REVERSÃO (baixa)" if bias_d1_msg == 'baixa' else "provável CONTINUAÇÃO (alta)"

        msg = f"🧲 <b>Manipulação detectada — {pair}</b>\n\n"
        msg += f"Zona D1: {round(zona['bottom'],6)} – {round(zona['top'],6)} ({zona['toques']} toques)\n"
        msg += f"Varreu o {lado_txt} da zona D1 em {round(sweep['nivel'],6)}\n"
        msg += f"📊 Viés Diário (D1): <b>{bias_d1_msg.upper()}</b>\n"
        msg += f"🔮 Expectativa (não garantida): {expectativa_txt}\n"
        msg += f"⏳ Aguardando confirmação real da distribuição..."
        send_telegram_fn(msg)

    choch = detect_choch_after_sweep(exec_candles, sweep)
    if not choch:
        resultado['motivo'] = 'sweep ok, mas CHoCH ainda não confirmou'
        _save_zone_state(db_file, pair, zona, 'sweep', now, sweep=sweep)
        return resultado

    if choch['index'] < len(exec_candles) and not candle_e_decisivo(exec_candles[choch['index']]):
        resultado['motivo'] = 'candle da quebra de estrutura (CHoCH) tem pavio grande — aguardando quebra mais decisiva'
        _save_zone_state(db_file, pair, zona, 'sweep', now, sweep=sweep)
        return resultado

    resultado['choch_nivel'] = round(choch['nivel'], 6)
    resultado['choch_direcao'] = choch['direcao']

    bias_d1 = compute_bias_from_swings(d1_candles)
    bias_h4 = compute_bias_from_swings(h4_candles) if h4_candles else 'neutro'
    resultado['bias_d1'] = bias_d1
    resultado['bias_h4'] = bias_h4

    contra_alta = choch['direcao'] == 'alta' and (bias_d1 == 'baixa' and bias_h4 == 'baixa')
    contra_baixa = choch['direcao'] == 'baixa' and (bias_d1 == 'alta' and bias_h4 == 'alta')
    if contra_alta or contra_baixa:
        resultado['motivo'] = (
            f"CHoCH confirmado, mas contra o bias dos timeframes maiores "
            f"(D1={bias_d1}, H4={bias_h4}) — descartado"
        )
        _save_zone_state(db_file, pair, zona, 'choch_contra_bias_maior', now, sweep=sweep, choch=choch)
        return resultado

    # ── AJUSTADO — pedido explícito do Juninho: "suporte + manipulação
    # = compra, simples assim", sem o RSI travar a entrada. O RSI volta
    # a ser INFORMATIVO (mostrado na mensagem, não bloqueia mais nada).
    # Continua contando como bônus dentro do compute_score (mais abaixo)
    # — só não impede mais o sinal de disparar sozinho. ──
    last_rsi = resultado['indicadores'].get('rsi14') if resultado.get('indicadores') else None
    resultado['rsi14'] = last_rsi
    rsi_contra_aviso = None
    if last_rsi is not None:
        if choch['direcao'] == 'alta' and last_rsi >= RSI_GATE_BLOQUEIA_LONG_ACIMA:
            rsi_contra_aviso = f"RSI={round(last_rsi,1)} já esticado pra cima"
        elif choch['direcao'] == 'baixa' and last_rsi <= RSI_GATE_BLOQUEIA_SHORT_ABAIXO:
            rsi_contra_aviso = f"RSI={round(last_rsi,1)} já esticado pra baixo"

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

    if not candle_e_decisivo(exec_candles[-1]):
        resultado['motivo'] = 'preço na zona de entrada, mas candle de confirmação indeciso (pavio grande) — aguardando'
        _save_zone_state(db_file, pair, zona, 'aguardando_candle_decisivo', now, sweep=sweep, choch=choch)
        return resultado

    score, detalhes = compute_score(zona, sweep, choch, entry_zone, exec_candles, na_killzone, indicadores=resultado.get('indicadores'))
    resultado['score'] = score
    resultado['detalhes'] = detalhes
    resultado['direcao'] = choch['direcao']

    sl = aplicar_buffer_stop_atr(sweep['nivel'], choch['direcao'], exec_candles)
    entry = melhor_preco_na_zona(entry_zone, choch['direcao'], preco_atual_fallback=preco_atual)
    risco = abs(entry - sl)
    tp = entry + risco * RR_TARGET_NORMAL if choch['direcao'] == 'alta' else entry - risco * RR_TARGET_NORMAL
    resultado['entry'] = round(entry, 6)
    resultado['sl'] = round(sl, 6)
    resultado['tp'] = round(tp, 6)

    if score >= SCORE_THRESHOLD_SINAL:
        # ── NOVO 05/08: gates estilo Vortex (Regime/R:R/Monte Carlo) —
        # rodam só quando o score já passou no threshold, pra não gastar
        # processamento à toa. Se algum gate falhar, o sinal NÃO dispara,
        # mesmo com score alto — igual a Vortex reporta "Filtros de
        # Qualidade" e bloqueia se algum não passar. ──
        gates_ok, gates = aplicar_gates_entrada(
            choch['direcao'], resultado['entry'], resultado['sl'], resultado['tp'],
            resultado.get('indicadores'), d1_candles,
        )
        resultado['gates'] = gates

        if not gates_ok:
            gates_falhos = [g['nome'] for g in gates if not g['passou']]
            resultado['motivo'] = f"score {score} ok, mas bloqueado por gate: {', '.join(gates_falhos)}"
            _save_zone_state(db_file, pair, zona, 'gate_bloqueou', now, sweep=sweep, choch=choch)
            return resultado

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
                regime_msg, adx_msg = compute_market_regime(d1_candles)
                candle_pat = (resultado.get('indicadores') or {}).get('candle_pattern')
                micro_bos = detect_micro_bos(exec_candles, choch['direcao'])
                msg = f"⚡ <b>Sinal Scalp Ao Vivo — {pair}</b>\n\n"
                msg += f"{arrow} <b>{'LONG' if choch['direcao']=='alta' else 'SHORT'}</b> | TF execução: {exec_tf_label}{kz_txt}\n"
                msg += f"📍 Entrada: {resultado['entry']}\n"
                msg += f"🛑 Stop: {resultado['sl']}\n"
                msg += f"✅ TP: {resultado['tp']}\n\n"
                msg += f"<b>Por que este sinal foi gerado:</b>\n"
                msg += _formatar_motivos_principais(
                    regime_msg, adx_msg, 'CHoCH', choch['direcao'], entry_zone['tipo'],
                    score, gates, candle_pattern=candle_pat, na_killzone=na_killzone, killzone_nome=killzone_nome,
                )
                if micro_bos['confirmado']:
                    msg += f"\n• Micro BOS: confirmado nas últimas {MICRO_BOS_LOOKBACK} velas (rompeu {micro_bos['nivel_rompido']})"
                msg += f"\n\n🎯 RSI: {round(last_rsi,1) if last_rsi is not None else 'N/D'} | Viés Diário: {bias_d1.upper()}"
                if rsi_contra_aviso:
                    msg += f"\n⚠️ Atenção: {rsi_contra_aviso} — entrada mesmo assim, confirme no gráfico antes"
                bloco_extra = montar_bloco_analise_extra(
                    db_file, pair, choch['direcao'], resultado['entry'], resultado['sl'], resultado['tp'],
                    'scalp_signal_state', sweep_nivel=sweep['nivel'], sweep_tipo=sweep['lado'],
                    exec_candles=exec_candles, entry_zone_tipo=entry_zone['tipo'],
                    obs_com_mitigacao=find_order_blocks_com_mitigacao(exec_candles),
                )
                if bloco_extra:
                    msg += f"\n\n{bloco_extra}"
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


def process_pair_scalp_filtros_shadow(db_file, pair, d1_candles, exec_candles, exec_tf_label, h4_candles=None):
    _resolver_filtros_shadow_pendentes(db_file, pair, exec_candles)

    preco_atual = exec_candles[-1]['c']

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

    entry_zone = find_fvg_ob_after_choch(exec_candles, choch, min_gap_pct=0)
    if not entry_zone:
        entry_zone = find_ifvg_after_choch(exec_candles, choch)
    if not entry_zone:
        entry_zone = find_breaker_block_after_choch(exec_candles, choch)
    if not entry_zone:
        return None

    if not price_in_zone(entry_zone, preco_atual):
        return None

    na_killzone, _ = is_in_killzone()
    indicadores = compute_technical_indicators(exec_candles)
    score, _ = compute_score(zona, sweep, choch, entry_zone, exec_candles, na_killzone, indicadores=indicadores)

    if score < SCORE_THRESHOLD_SINAL:
        return None

    filtros_bloqueados = []

    if choch['index'] < len(exec_candles) and not candle_e_decisivo(exec_candles[choch['index']]):
        filtros_bloqueados.append('candle_choch_indeciso')

    bias_d1 = compute_bias_from_swings(d1_candles)
    bias_h4 = compute_bias_from_swings(h4_candles) if h4_candles else 'neutro'
    contra_alta = choch['direcao'] == 'alta' and (bias_d1 == 'baixa' and bias_h4 == 'baixa')
    contra_baixa = choch['direcao'] == 'baixa' and (bias_d1 == 'alta' and bias_h4 == 'alta')
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
        return None

    entry = melhor_preco_na_zona(entry_zone, choch['direcao'], preco_atual_fallback=preco_atual)
    sl = aplicar_buffer_stop_atr(sweep['nivel'], choch['direcao'], exec_candles)
    risco = abs(entry - sl)
    tp = entry + risco * RR_TARGET_NORMAL if choch['direcao'] == 'alta' else entry - risco * RR_TARGET_NORMAL

    resultado = {
        'pair': pair, 'exec_tf': exec_tf_label, 'modo': 'filtros_shadow',
        'direcao': choch['direcao'], 'score': score,
        'entry': round(entry, 6), 'sl': round(sl, 6), 'tp': round(tp, 6),
        'filtros_que_bloqueariam': filtros_bloqueados,
    }
    _save_filtro_shadow(db_file, pair, exec_tf_label, choch['direcao'], score, resultado['entry'], resultado['sl'], resultado['tp'], filtros_bloqueados)
    return resultado


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


def process_pair_scalp_continuacao(db_file, pair, d1_candles, exec_candles, exec_tf_label, send_telegram_fn=None, h4_candles=None):
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
        'rsi14': None,
        'entry_zone_top': None,
        'entry_zone_bottom': None,
        'entry_zone_tipo': None,
        'indicadores': None,
        'gates': [],
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

    saved = _load_saved_state(db_file, pair, table='scalp_zone_state_continuacao')

    # ── AJUSTADO 05/08: o aviso "Possível Acumulação" já é enviado pelo
    # modo Normal (process_pair_scalp), que usa a MESMA zona D1 (mesma
    # compute_d1_zones). Os dois modos rodando em paralelo mandavam o
    # mesmo aviso duas vezes pro mesmo par. Aqui só rastreia
    # internamente (zona_e_nova continua calculada, só não dispara
    # Telegram) — não perde nenhuma lógica de entrada, só para de
    # duplicar mensagem. ──
    zona_e_nova = (
        not saved or saved.get('zona_top') is None
        or abs((saved.get('zona_top') or 0) - zona['top']) > (zona['top'] - zona['bottom'])
        or abs((saved.get('zona_bottom') or 0) - zona['bottom']) > (zona['top'] - zona['bottom'])
    )

    fresh_sweep = detect_sweep_in_zone(exec_candles, zona)

    # ── NOVO — mesma checagem de liquidez real do modo Normal. ──
    if fresh_sweep:
        tipo_liq = 'high' if fresh_sweep['lado'] == 'alta' else 'low'
        liq_valor, _liq_idx = find_liquidez_antiga(exec_candles, fresh_sweep['index'], tipo_liq)
        if liq_valor is None:
            fresh_sweep = None

    saved_valido = _sweep_ainda_valido(saved, zona, now)

    sweep = None
    if fresh_sweep and (not saved_valido or fresh_sweep['t'] >= saved['sweep_ts']):
        sweep = fresh_sweep
    elif saved_valido:
        sweep = {'t': saved['sweep_ts'], 'nivel': saved['sweep_nivel'], 'lado': saved['sweep_lado']}

    if not sweep:
        resultado['motivo'] = 'sem sweep de liquidez real detectado ainda'
        _save_zone_state(db_file, pair, zona, 'zona', now, table='scalp_zone_state_continuacao')
        return resultado

    resultado['sweep_nivel'] = round(sweep['nivel'], 6)
    resultado['sweep_lado'] = sweep['lado']

    # ── AJUSTADO 05/08: aviso "Manipulação detectada" idem — já sai do
    # modo Normal pra essa mesma zona/sweep, então aqui só rastreia
    # (sweep_e_novo), sem mandar Telegram de novo. ──
    sweep_e_novo = not saved or saved.get('sweep_ts') != sweep.get('t')

    bos = detect_bos_continuation_after_sweep(exec_candles, sweep)
    if not bos:
        resultado['motivo'] = 'sweep ok, mas BOS de continuação ainda não confirmou'
        _save_zone_state(db_file, pair, zona, 'sweep', now, sweep=sweep, table='scalp_zone_state_continuacao')
        return resultado

    if bos['index'] < len(exec_candles) and not candle_e_decisivo(exec_candles[bos['index']]):
        resultado['motivo'] = 'candle da quebra de estrutura (BOS) tem pavio grande — aguardando quebra mais decisiva'
        _save_zone_state(db_file, pair, zona, 'sweep', now, sweep=sweep, table='scalp_zone_state_continuacao')
        return resultado

    resultado['bos_nivel'] = round(bos['nivel'], 6)
    resultado['bos_direcao'] = bos['direcao']

    bias_d1 = compute_bias_from_swings(d1_candles)
    bias_h4 = compute_bias_from_swings(h4_candles) if h4_candles else 'neutro'
    resultado['bias_d1'] = bias_d1
    resultado['bias_h4'] = bias_h4

    contra_alta = bos['direcao'] == 'alta' and (bias_d1 == 'baixa' and bias_h4 == 'baixa')
    contra_baixa = bos['direcao'] == 'baixa' and (bias_d1 == 'alta' and bias_h4 == 'alta')
    if contra_alta or contra_baixa:
        resultado['motivo'] = (
            f"BOS confirmado, mas contra o bias dos timeframes maiores "
            f"(D1={bias_d1}, H4={bias_h4}) — descartado"
        )
        _save_zone_state(db_file, pair, zona, 'bos_contra_bias_maior', now, sweep=sweep, choch=bos, table='scalp_zone_state_continuacao')
        return resultado

    # ── AJUSTADO — mesmo critério do modo Normal: RSI volta a ser
    # INFORMATIVO, não bloqueia mais a entrada. ──
    last_rsi = resultado['indicadores'].get('rsi14') if resultado.get('indicadores') else None
    resultado['rsi14'] = last_rsi
    rsi_contra_aviso = None
    if last_rsi is not None:
        if bos['direcao'] == 'alta' and last_rsi >= RSI_GATE_BLOQUEIA_LONG_ACIMA:
            rsi_contra_aviso = f"RSI={round(last_rsi,1)} já esticado pra cima"
        elif bos['direcao'] == 'baixa' and last_rsi <= RSI_GATE_BLOQUEIA_SHORT_ABAIXO:
            rsi_contra_aviso = f"RSI={round(last_rsi,1)} já esticado pra baixo"

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

    if not candle_e_decisivo(exec_candles[-1]):
        resultado['motivo'] = 'preço na zona de entrada, mas candle de confirmação indeciso (pavio grande) — aguardando'
        _save_zone_state(db_file, pair, zona, 'aguardando_candle_decisivo', now, sweep=sweep, choch=bos, table='scalp_zone_state_continuacao')
        return resultado

    score, detalhes = compute_score(zona, sweep, bos, entry_zone, exec_candles, na_killzone, indicadores=resultado.get('indicadores'))
    resultado['score'] = score
    resultado['detalhes'] = detalhes
    resultado['direcao'] = bos['direcao']

    sl = aplicar_buffer_stop_atr(sweep['nivel'], bos['direcao'], exec_candles)
    entry = melhor_preco_na_zona(entry_zone, bos['direcao'], preco_atual_fallback=preco_atual)
    risco = abs(entry - sl)
    tp = entry + risco * RR_TARGET_CONTINUACAO if bos['direcao'] == 'alta' else entry - risco * RR_TARGET_CONTINUACAO
    resultado['entry'] = round(entry, 6)
    resultado['sl'] = round(sl, 6)
    resultado['tp'] = round(tp, 6)

    if score >= SCORE_THRESHOLD_SINAL:
        gates_ok, gates = aplicar_gates_entrada(
            bos['direcao'], resultado['entry'], resultado['sl'], resultado['tp'],
            resultado.get('indicadores'), d1_candles,
        )
        resultado['gates'] = gates

        if not gates_ok:
            gates_falhos = [g['nome'] for g in gates if not g['passou']]
            resultado['motivo'] = f"score {score} ok, mas bloqueado por gate: {', '.join(gates_falhos)}"
            _save_zone_state(db_file, pair, zona, 'gate_bloqueou', now, sweep=sweep, choch=bos, table='scalp_zone_state_continuacao')
            return resultado

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
                regime_msg, adx_msg = compute_market_regime(d1_candles)
                candle_pat = (resultado.get('indicadores') or {}).get('candle_pattern')
                micro_bos = detect_micro_bos(exec_candles, bos['direcao'])
                msg = f"🔁 <b>Sinal Scalp Continuação — {pair}</b>\n\n"
                msg += f"{arrow} <b>{'LONG' if bos['direcao']=='alta' else 'SHORT'}</b> | TF execução: {exec_tf_label}{kz_txt}\n"
                msg += f"📍 Entrada: {resultado['entry']}\n"
                msg += f"🛑 Stop: {resultado['sl']}\n"
                msg += f"✅ TP: {resultado['tp']}\n\n"
                msg += f"<b>Por que este sinal foi gerado:</b>\n"
                msg += _formatar_motivos_principais(
                    regime_msg, adx_msg, 'BOS (continuação)', bos['direcao'], entry_zone['tipo'],
                    score, gates, candle_pattern=candle_pat, na_killzone=na_killzone, killzone_nome=killzone_nome,
                )
                if micro_bos['confirmado']:
                    msg += f"\n• Micro BOS: confirmado nas últimas {MICRO_BOS_LOOKBACK} velas (rompeu {micro_bos['nivel_rompido']})"
                msg += f"\n\n🎯 RSI: {round(last_rsi,1) if last_rsi is not None else 'N/D'} | Viés Diário: {bias_d1.upper()}"
                if rsi_contra_aviso:
                    msg += f"\n⚠️ Atenção: {rsi_contra_aviso} — entrada mesmo assim, confirme no gráfico antes"
                bloco_extra = montar_bloco_analise_extra(
                    db_file, pair, bos['direcao'], resultado['entry'], resultado['sl'], resultado['tp'],
                    'scalp_signal_state_continuacao', sweep_nivel=sweep['nivel'], sweep_tipo=sweep['lado'],
                    exec_candles=exec_candles, entry_zone_tipo=entry_zone['tipo'],
                    obs_com_mitigacao=find_order_blocks_com_mitigacao(exec_candles),
                )
                if bloco_extra:
                    msg += f"\n\n{bloco_extra}"
                send_telegram_fn(msg)
    else:
        resultado['motivo'] = f'score {score} abaixo de {SCORE_THRESHOLD_SINAL} — sem entrada'
        _save_zone_state(db_file, pair, zona, 'score_insuficiente', now, sweep=sweep, choch=bos, table='scalp_zone_state_continuacao')

    return resultado


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


# ── NOVO 05/08: Break Even + Parcial automatizados, inspirado direto
# no que a Vortex mostra ("BE +6.0 pips", depois "PARCIAL +6.0 pips").
# O Kairos não executa ordens de verdade (é sistema de alerta via
# Telegram, não corretora), então esse monitoramento manda uma MENSAGEM
# avisando o Juninho pra mover o stop / realizar parte da posição
# manualmente — não move nada sozinho na exchange. Roda 1x por ciclo,
# por par, em cima do TF de execução (mesmos candles já buscados no
# ciclo, sem call extra à Bybit).
#
# Lógica: quando o preço andar BE_PROGRESSO_PCT do caminho até o TP
# (padrão 30%), avisa mover o stop pra entrada (trava risco em zero).
# Quando andar PARCIAL_PROGRESSO_PCT (padrão 50%), avisa realizar
# parcial (50% da posição) e deixar o resto correr — igual às "Regras
# de Ouro" que o próprio Kairos já recomenda em texto no ICT, agora
# ativamente monitorado nos modos de scalp ao vivo. ──
BE_PROGRESSO_PCT = 0.30
PARCIAL_PROGRESSO_PCT = 0.50


def _progresso_ate_tp(preco_atual, entry, sl, tp, direcao):
    """Retorna 0-1+ representando quanto do caminho até o TP já foi
    percorrido. >=1 significa que já bateu o TP; <0 significa que já
    foi contra a entrada (pode já ter batido o SL)."""
    dist_total = abs(tp - entry)
    if dist_total <= 0:
        return 0
    avanco = (preco_atual - entry) if direcao == 'alta' else (entry - preco_atual)
    return avanco / dist_total


def gerenciar_trades_abertos(db_file, pair, exec_candles, table, send_telegram_fn=None,
                              be_progresso_pct=BE_PROGRESSO_PCT, parcial_progresso_pct=PARCIAL_PROGRESSO_PCT):
    """
    Chamar 1x por ciclo, por par, por tabela de sinal (scalp_signal_
    state, scalp_signal_state_continuacao, scalp_rapido_signal_state,
    scalp_cascata_signal_state, scalp_antecipado_signal_state,
    scalp_indicadores_signal_state). Verifica trades alertados
    (alerted=1) ainda pendentes (resultado_final='pendente') desse par
    nessa tabela, e avisa BE/Parcial conforme o progresso do preço.
    """
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

        # Checa se já bateu SL ou TP de verdade nos candles recentes —
        # se sim, fecha o trade (resultado_final) e não manda mais
        # aviso de BE/Parcial pra ele.
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


def process_pair_scalp_rapido(db_file, pair, d1_candles, exec_candles, exec_tf_label, send_telegram_fn=None):
    preco_atual = exec_candles[-1]['c']

    resultado = {
        'pair': pair, 'exec_tf': exec_tf_label, 'modo': 'scalp_rapido',
        'sinal': False, 'direcao': None, 'entry': None, 'sl': None, 'tp': None,
        'zona_tipo': None, 'motivo': None, 'em_cooldown': False, 'rsi': None,
        'resistencia_top': None, 'resistencia_bottom': None,
        'suporte_top': None, 'suporte_bottom': None,
        'zona_movel_top': None, 'zona_movel_bottom': None, 'zona_movel_largura_pct': None,
        'gates': [],
    }

    zona_diaria = compute_zona_diaria_movel(d1_candles)
    if not zona_diaria:
        resultado['motivo'] = 'sem candle D1 anterior disponível ainda'
        return resultado

    resultado['resistencia_top'] = round(zona_diaria['resistencia']['top'], 6)
    resultado['resistencia_bottom'] = round(zona_diaria['resistencia']['bottom'], 6)
    resultado['suporte_top'] = round(zona_diaria['suporte']['top'], 6)
    resultado['suporte_bottom'] = round(zona_diaria['suporte']['bottom'], 6)

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

    candles_pre_sweep = exec_candles[:sweep['index']]
    zona_movel_pre_sweep = compute_zona_movel(candles_pre_sweep) if candles_pre_sweep else None
    if zona_movel_pre_sweep and zona_movel_pre_sweep['largura_pct'] > ZONA_MOVEL_MAX_LARGURA_PCT:
        resultado['motivo'] = (
            f"sweep e candle ok, mas região móvel (antes do sweep) larga demais "
            f"({round(zona_movel_pre_sweep['largura_pct']*100, 2)}% > {ZONA_MOVEL_MAX_LARGURA_PCT*100}%) "
            f"— preço não estava lateralizado o suficiente antes do sweep"
        )
        return resultado

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
    sl = aplicar_buffer_stop_atr(sweep['nivel'], direcao, exec_candles)
    risco = abs(entry - sl)
    tp = entry + risco * RR_TARGET_RAPIDO if direcao == 'alta' else entry - risco * RR_TARGET_RAPIDO

    # ── NOVO 05/08: só o gate de RR aqui (sem Regime, sem Monte Carlo)
    # — esse modo existe pra ser ágil, sweep + RSI extremo já é a
    # própria regra de entrada, checar regime mataria o propósito de
    # pegar reversão rápida em qualquer condição de mercado. ──
    gates_ok, gates = aplicar_gates_entrada(
        direcao, entry, sl, tp, None, d1_candles, incluir_regime=False,
    )
    resultado['gates'] = gates
    if not gates_ok:
        gates_falhos = [g['nome'] for g in gates if not g['passou']]
        resultado['motivo'] = f"sweep+RSI ok, mas bloqueado por gate: {', '.join(gates_falhos)}"
        return resultado

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
        msg += f"📍 Entrada: {resultado['entry']}\n🛑 Stop (curto): {resultado['sl']}\n✅ TP (RR {RR_TARGET_RAPIDO}:1): {resultado['tp']}\n\n"
        msg += "<b>Sem CHoCH — entrada rápida no sweep + RSI extremo, stop curto, reentrada permitida em 5min.</b>"
        bloco_extra = montar_bloco_analise_extra(
            db_file, pair, direcao, resultado['entry'], resultado['sl'], resultado['tp'],
            'scalp_rapido_signal_state', sweep_nivel=sweep['nivel'], sweep_tipo=sweep['lado'],
            exec_candles=exec_candles,
        )
        if bloco_extra:
            msg += f"\n\n{bloco_extra}"
        send_telegram_fn(msg)
    elif em_cooldown:
        restante_min = (SCALP_RAPIDO_COOLDOWN_SECONDS - segundos_desde) // 60
        resultado['motivo'] = f'entrada_confirmada, mas em cooldown ({restante_min}min restantes)'

    return resultado


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


def process_pair_cascata_smc(db_file, pair, w_candles, d1_candles, h4_candles, h1_candles,
                              exec_candles, exec_tf_label, send_telegram_fn=None):
    resultado = {
        'pair': pair, 'exec_tf': exec_tf_label, 'modo': 'cascata_smc',
        'sinal': False, 'direcao': None, 'entry': None, 'sl': None, 'tp': None,
        'bias_semanal': None, 'bias_d1': None, 'bias_h4': None, 'bias_h1': None,
        'evento_tipo': None, 'motivo': None, 'em_cooldown': False,
        'zona_top': None, 'zona_bottom': None, 'zona_ativa': False,
        'sweep_nivel': None, 'sweep_lado': None,
        'choch_nivel': None, 'choch_direcao': None,
        'entry_zone_top': None, 'entry_zone_bottom': None, 'entry_zone_tipo': None,
        'score': 0, 'na_killzone': False, 'killzone_nome': None, 'indicadores': None,
        'mtf_status': None, 'gates': [],
    }

    try:
        resultado['indicadores'] = compute_technical_indicators(exec_candles)
    except Exception as e:
        print(f"[scalp_engine] erro ao calcular indicadores (cascata) de {pair}: {e}")

    na_killzone, killzone_nome = is_in_killzone()
    resultado['na_killzone'] = na_killzone
    resultado['killzone_nome'] = killzone_nome

    bias_w = compute_lux_structure_bias(w_candles) if w_candles else 'neutro'
    bias_d1 = compute_lux_structure_bias(d1_candles)
    bias_h4 = compute_lux_structure_bias(h4_candles) if h4_candles else 'neutro'
    bias_h1 = compute_lux_structure_bias(h1_candles) if h1_candles else 'neutro'
    resultado.update({
        'bias_semanal': bias_w, 'bias_d1': bias_d1, 'bias_h4': bias_h4, 'bias_h1': bias_h1,
    })

    # ── AJUSTADO 05/08: antes exigia os 4 timeframes (Semanal/D1/H4/H1)
    # 100% iguais pra disparar qualquer sinal — muito rígido. Visto na
    # Vortex que ela dispara sinal mesmo com "MTF: PARTIAL" (nem todos
    # os timeframes alinhados) e o resultado real conferido (USDCAD)
    # foi bom. Agora aceita FULL (4/4) ou PARTIAL (3/4, com o 4º neutro
    # ou contra) — PARTIAL fica marcado no resultado e na mensagem,
    # não é escondido. ──
    biases = [bias_w, bias_d1, bias_h4, bias_h1]
    contagem_alta = biases.count('alta')
    contagem_baixa = biases.count('baixa')

    if contagem_alta == 4:
        direcao_macro, mtf_status = 'alta', 'FULL'
    elif contagem_baixa == 4:
        direcao_macro, mtf_status = 'baixa', 'FULL'
    elif contagem_alta == 3:
        direcao_macro, mtf_status = 'alta', 'PARTIAL'
    elif contagem_baixa == 3:
        direcao_macro, mtf_status = 'baixa', 'PARTIAL'
    else:
        resultado['motivo'] = (
            f"timeframes maiores não alinhados o suficiente "
            f"(Semanal={bias_w}, D1={bias_d1}, H4={bias_h4}, H1={bias_h1})"
        )
        return resultado

    resultado['mtf_status'] = mtf_status

    zona_diaria = compute_zona_diaria_movel(d1_candles)
    if not zona_diaria:
        resultado['motivo'] = 'timeframes alinhados, mas sem candle D1 anterior disponível'
        return resultado

    zona_visual = zona_diaria['suporte'] if direcao_macro == 'alta' else zona_diaria['resistencia']
    resultado['zona_top'] = round(zona_visual['top'], 6)
    resultado['zona_bottom'] = round(zona_visual['bottom'], 6)
    resultado['zona_ativa'] = True

    now = int(time.time())

    fresh_sweep = detect_sweep_zona_diaria_movel(exec_candles, zona_diaria)

    zona_para_validar = None
    if fresh_sweep:
        zona_para_validar = (
            zona_diaria['resistencia'] if fresh_sweep['tipo_zona'] == 'resistencia' else zona_diaria['suporte']
        )
    else:
        zona_para_validar = zona_diaria['suporte']

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
            'index': len(exec_candles) - 1,
        }

    if not sweep:
        resultado['motivo'] = f'timeframes alinhados em {direcao_macro}, mas sem sweep na zona diária ainda'
        _save_zone_state(db_file, pair, {'top': zona_para_validar['top'], 'bottom': zona_para_validar['bottom']},
                          'zona', now, table='scalp_cascata_zone_state')
        return resultado

    zona_do_sweep = zona_diaria['resistencia'] if sweep['tipo_zona'] == 'resistencia' else zona_diaria['suporte']
    _save_zone_state(
        db_file, pair, {'top': zona_do_sweep['top'], 'bottom': zona_do_sweep['bottom']},
        'sweep', now, sweep=sweep, table='scalp_cascata_zone_state',
    )

    resultado['sweep_nivel'] = round(sweep['nivel'], 6)
    resultado['sweep_lado'] = sweep['lado']

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
    resultado['choch_nivel'] = round(evento['nivel'], 6)
    resultado['choch_direcao'] = evento['direcao']

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
    sl = aplicar_buffer_stop_atr(sweep['nivel'], direcao_macro, exec_candles)
    risco = abs(entry - sl)
    tp = entry + risco * RR_TARGET_CASCATA if direcao_macro == 'alta' else entry - risco * RR_TARGET_CASCATA

    # ── NOVO 05/08: gates RR + Monte Carlo (sem Regime — os 4
    # timeframes alinhados já são um filtro de tendência mais rigoroso
    # que ADX sozinho, adicionar Regime aqui seria redundante). ──
    gates_ok, gates = aplicar_gates_entrada(
        direcao_macro, entry, sl, tp, resultado.get('indicadores'), d1_candles, incluir_regime=False,
    )
    resultado['gates'] = gates
    if not gates_ok:
        gates_falhos = [g['nome'] for g in gates if not g['passou']]
        resultado['motivo'] = f"timeframes alinhados ({mtf_status}), mas bloqueado por gate: {', '.join(gates_falhos)}"
        return resultado

    resultado.update({
        'sinal': True,
        'direcao': direcao_macro,
        'entry': round(entry, 6),
        'sl': round(sl, 6),
        'tp': round(tp, 6),
        'motivo': 'entrada_confirmada',
        'score': 100 if mtf_status == 'FULL' else 85,
    })

    segundos_desde = _segundos_desde_ultimo_alerta(db_file, 'scalp_cascata_signal_state', pair)
    em_cooldown = segundos_desde is not None and segundos_desde < CASCATA_COOLDOWN_SECONDS
    resultado['em_cooldown'] = em_cooldown

    _save_cascata_signal(db_file, pair, exec_tf_label, resultado, alerted=not em_cooldown)

    if send_telegram_fn and not em_cooldown:
        arrow = '📈' if direcao_macro == 'alta' else '📉'
        label = 'LONG' if direcao_macro == 'alta' else 'SHORT'
        mtf_txt = "todos os 4 timeframes alinhados" if mtf_status == 'FULL' else "3 de 4 timeframes alinhados (parcial)"
        msg = f"🌊 <b>Cascata SMC — {pair}</b>\n\n"
        msg += f"{arrow} <b>{label}</b> | TF execução: {exec_tf_label}\n"
        msg += f"📊 Alinhamento: Semanal={bias_w} | D1={bias_d1} | H4={bias_h4} | H1={bias_h1} | MTF: {mtf_status}\n"
        msg += f"🔨 Confirmação: {evento_tipo}\n"
        msg += f"📍 Entrada: {resultado['entry']}\n🛑 Stop: {resultado['sl']}\n✅ TP (RR {RR_TARGET_CASCATA}:1): {resultado['tp']}\n\n"
        msg += f"<b>{mtf_txt} — sweep na zona diária + confirmação de estrutura.</b>"
        bloco_extra = montar_bloco_analise_extra(
            db_file, pair, direcao_macro, resultado['entry'], resultado['sl'], resultado['tp'],
            'scalp_cascata_signal_state', sweep_nivel=sweep['nivel'], sweep_tipo=sweep['lado'],
            exec_candles=exec_candles,
        )
        if bloco_extra:
            msg += f"\n\n{bloco_extra}"
        send_telegram_fn(msg)
    elif em_cooldown:
        restante_min = (CASCATA_COOLDOWN_SECONDS - segundos_desde) // 60
        resultado['motivo'] = f'entrada_confirmada, mas em cooldown ({restante_min}min restantes)'

    return resultado


RSI_EXTREMO_BAIXA = 20
RSI_EXTREMO_ALTA = 80
LIQUIDEZ_LOOKBACK = 40
RR_FIXO_ANTECIPADO = 2.0
ANTECIPADO_SWEEP_LOOKBACK = 10


def find_liquidez_antiga(exec_candles, ate_index, tipo, lookback=LIQUIDEZ_LOOKBACK):
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


def detect_sweep_liquidez_antiga(exec_candles, zona, lookback=ANTECIPADO_SWEEP_LOOKBACK):
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
    preco_atual = exec_candles[-1]['c']
    bandas = compute_d1_zones(d1_candles)
    zona = find_active_zone(bandas, preco_atual)

    resultado = {
        'pair': pair, 'exec_tf': exec_tf_label, 'modo': 'antecipado_v2',
        'sinal': False, 'direcao': None, 'entry': None, 'sl': None, 'tp': None,
        'rsi': None, 'liquidez_varrida': None, 'divergencia_rsi': False,
        'bias_d1': None, 'bias_h4': None, 'motivo': None, 'indicadores': None,
        'em_cooldown': False, 'gates': [],
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

    bias_d1 = compute_bias_from_swings(d1_candles)
    bias_h4 = compute_bias_from_swings(h4_candles) if h4_candles else 'neutro'
    resultado['bias_d1'] = bias_d1
    resultado['bias_h4'] = bias_h4

    contra_alta = direcao == 'alta' and (bias_d1 == 'baixa' and bias_h4 == 'baixa')
    contra_baixa = direcao == 'baixa' and (bias_d1 == 'alta' and bias_h4 == 'alta')
    if contra_alta or contra_baixa:
        resultado['rsi'] = round(rsi_val, 1)
        resultado['motivo'] = (
            f"sweep+RSI ok, mas timeframes maiores contra a direção "
            f"(D1={bias_d1}, H4={bias_h4}) — sinal descartado"
        )
        return resultado

    tem_divergencia = check_rsi_divergence(exec_candles, sweep)

    entry = preco_atual
    sl = aplicar_buffer_stop_atr(sweep['nivel_pavio'], direcao, exec_candles)
    risco = abs(entry - sl)
    tp = entry - risco * RR_FIXO_ANTECIPADO if direcao == 'baixa' else entry + risco * RR_FIXO_ANTECIPADO

    # ── NOVO 05/08: gates RR + Monte Carlo (SEM Regime — esse modo
    # existe justamente pra caçar reversão em RSI extremo, que muitas
    # vezes acontece dentro de ranging, não em tendência). ──
    gates_ok, gates = aplicar_gates_entrada(
        direcao, entry, sl, tp, resultado.get('indicadores'), d1_candles, incluir_regime=False,
    )
    resultado['gates'] = gates
    if not gates_ok:
        gates_falhos = [g['nome'] for g in gates if not g['passou']]
        resultado['rsi'] = round(rsi_val, 1)
        resultado['motivo'] = f"sweep+RSI ok, mas bloqueado por gate: {', '.join(gates_falhos)}"
        return resultado

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
        msg += f"📍 Entrada: {resultado['entry']}\n🛑 Stop: {resultado['sl']}\n✅ TP (RR {RR_FIXO_ANTECIPADO}:1): {resultado['tp']}\n\n"
        msg += "<b>Sem CHoCH confirmado — entrada agressiva, posição menor recomendada.</b>"
        bloco_extra = montar_bloco_analise_extra(
            db_file, pair, direcao, resultado['entry'], resultado['sl'], resultado['tp'],
            'scalp_antecipado_signal_state', sweep_nivel=sweep['nivel_pavio'], sweep_tipo=sweep['lado'],
            exec_candles=exec_candles,
        )
        if bloco_extra:
            msg += f"\n\n{bloco_extra}"
        send_telegram_fn(msg)
    elif em_cooldown and not ja_alertado_preco:
        restante_min = (COOLDOWN_SECONDS - segundos_desde) // 60
        resultado['motivo'] = f'entrada_confirmada, mas em cooldown ({restante_min}min restantes)'

    return resultado


VOTOS_MINIMOS_SINAL = 7
ATR_MULT_STOP = 1.5
RR_INDICADORES = 2.0


def _votos_indicadores(indicadores, preco_atual):
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
    if vwap is not None:
        votos.append(('vwap', 'alta' if preco_atual > vwap else 'baixa'))

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


def process_pair_scalp_indicadores(db_file, pair, exec_candles, exec_tf_label, send_telegram_fn=None, d1_candles=None):
    preco_atual = exec_candles[-1]['c']
    indicadores = compute_technical_indicators(exec_candles)

    resultado = {
        'pair': pair, 'exec_tf': exec_tf_label, 'modo': 'confluencia_indicadores',
        'sinal': False, 'direcao': None, 'entry': None, 'sl': None, 'tp': None,
        'stop_via': None,
        'score': 0, 'votos_favor': 0, 'votos_total': 0, 'votos_detalhe': [],
        'tendencia_forte': False, 'indicadores': indicadores, 'em_cooldown': False,
        'motivo': None, 'gates': [],
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

    # ── NOVO — pedido explícito: bloqueio de venda em zona de DEMANDA
    # D1 e de compra em zona de OFERTA D1. Esse modo (Confluência de
    # Indicadores) é o único dos 6 que NÃO depende de zona D1 por
    # padrão — vota só em indicadores técnicos, então sem essa
    # checagem ele podia (em teoria) vender bem em cima de um suporte
    # D1 forte, contra a regra de ouro que os outros modos já
    # respeitam estruturalmente. Se d1_candles não for passado
    # (chamada antiga sem esse parâmetro), a checagem simplesmente não
    # roda — não quebra nada que já funcionava. ──
    if d1_candles:
        bandas_d1 = compute_d1_zones(d1_candles)
        zona_htf = find_active_zone(bandas_d1, preco_atual)
        if zona_htf:
            tipo_zona = zona_htf.get('tipo_predominante')
            if direcao == 'baixa' and tipo_zona == 'demanda':
                resultado['motivo'] = (
                    f"{votos_favor}/{total} indicadores concordando em baixa, mas preço está "
                    f"dentro de zona de DEMANDA D1 ({round(zona_htf['bottom'],6)}-{round(zona_htf['top'],6)}, "
                    f"{zona_htf['toques']} toques) — short bloqueado (nunca vende no suporte)"
                )
                return resultado
            if direcao == 'alta' and tipo_zona == 'oferta':
                resultado['motivo'] = (
                    f"{votos_favor}/{total} indicadores concordando em alta, mas preço está "
                    f"dentro de zona de OFERTA D1 ({round(zona_htf['bottom'],6)}-{round(zona_htf['top'],6)}, "
                    f"{zona_htf['toques']} toques) — long bloqueado (nunca compra na resistência)"
                )
                return resultado

    entry = preco_atual

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
        sl = aplicar_buffer_stop_atr(sl_bruto, direcao, exec_candles)

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

    # ── NOVO 05/08: gates estilo Vortex — esse é o único dos 6 modos
    # sem zona D1/sweep/CHoCH por padrão (vota só em indicadores), então
    # é o que mais se beneficia do Gate de Regime (evita votar tendência
    # num mercado sem tendência nenhuma). Roda os 3 gates (Regime, RR,
    # Monte Carlo) — se algum falhar, sinal não dispara mesmo com
    # maioria de votos. ──
    candles_regime = d1_candles if d1_candles else exec_candles
    gates_ok, gates = aplicar_gates_entrada(direcao, entry, sl, tp, indicadores, candles_regime)
    resultado['gates'] = gates
    if not gates_ok:
        gates_falhos = [g['nome'] for g in gates if not g['passou']]
        resultado['motivo'] = f"{votos_favor}/{total} indicadores concordando em {direcao}, mas bloqueado por gate: {', '.join(gates_falhos)}"
        resultado['votos_favor'] = votos_favor
        return resultado

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
        msg += f"📍 Entrada: {resultado['entry']}\n🛑 {stop_label}: {resultado['sl']}\n✅ TP (RR {RR_INDICADORES}:1): {resultado['tp']}\n\n"
        msg += "<b>Sem zona D1/sweep/CHoCH — setup baseado só em indicadores técnicos, posição menor recomendada.</b>"
        bloco_extra = montar_bloco_analise_extra(
            db_file, pair, direcao, resultado['entry'], resultado['sl'], resultado['tp'],
            'scalp_indicadores_signal_state', exec_candles=exec_candles,
        )
        if bloco_extra:
            msg += f"\n\n{bloco_extra}"
        send_telegram_fn(msg)
    elif em_cooldown:
        restante_min = (COOLDOWN_SECONDS - segundos_desde) // 60
        resultado['motivo'] += f' (em cooldown, {restante_min}min restantes)'

    return resultado
