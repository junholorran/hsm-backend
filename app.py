from flask import Flask, request, jsonify, send_from_directory
import anthropic
import os
import time
import requests
import sqlite3
import re

app = Flask(__name__)
client = anthropic.Anthropic(api_key=os.environ.get('ANTHROPIC_API_KEY'))

TELEGRAM_TOKEN = os.environ.get('TELEGRAM_TOKEN')
TELEGRAM_CHAT_ID = os.environ.get('TELEGRAM_CHAT_ID')
DB_FILE = 'alerts.db'

PRECOS_TICKER = {}

# --- REGEX BLINDADAS CONTRA HTML E ESPAÇOS ---
RE_SCORE = re.compile(r'SCORE\s*OPERACIONAL\s*:[^\d]*(\d{1,3})\s*/\s*100', re.IGNORECASE)
RE_SL = re.compile(r'Stop\s*Loss\s*[^:]*:[^\d]*\$?\s*([\d,.]+)', re.IGNORECASE)
RE_TP = re.compile(r'Take\s*Profit\s*\d?\s*[^:]*:[^\d]*\$?\s*([\d,.]+)', re.IGNORECASE)
RE_ENTRY = re.compile(r'Entrada\s*Conservadora\s*[^:\n]{0,30}:[^\d]*\$?\s*([\d,.]+)', re.IGNORECASE)
RE_STYLE = re.compile(r'(scalp|swing|intraday)', re.IGNORECASE)
TIMEFRAMES_MAP = ["D1", "H4", "H1", "M15", "M5", "M1"]

# --- NOVO: BLOCO MÁQUINA — fonte de verdade para direção/score/entry ---
# O prompt força o modelo a terminar SEMPRE com este bloco, sem HTML, sem
# frases soltas. Isso elimina a contagem de palavras (bug do badge).
RE_DIRECAO_FINAL = re.compile(r'DIRECAO_FINAL\s*:\s*(LONG|SHORT|NEUTRO)', re.IGNORECASE)
RE_SCORE_FINAL = re.compile(r'SCORE_FINAL\s*:\s*(\d{1,3})', re.IGNORECASE)
RE_ENTRY_FINAL = re.compile(r'ENTRY_FINAL\s*:\s*\$?\s*([\d,.]+)', re.IGNORECASE)
RE_SL_FINAL = re.compile(r'SL_FINAL\s*:\s*\$?\s*([\d,.]+)', re.IGNORECASE)
RE_TP1_FINAL = re.compile(r'TP1_FINAL\s*:\s*\$?\s*([\d,.]+)', re.IGNORECASE)
RE_TP2_FINAL = re.compile(r'TP2_FINAL\s*:\s*\$?\s*([\d,.]+)', re.IGNORECASE)
RE_TP3_FINAL = re.compile(r'TP3_FINAL\s*:\s*\$?\s*([\d,.]+)', re.IGNORECASE)


def extract_trade_info(analysis, timeframes_str):
    """
    Extrai direção/score/entry/SL/TPs da análise.
    PRIORIDADE 1: bloco máquina estruturado (BLOCO_DADOS no fim da resposta).
    PRIORIDADE 2 (fallback, só se bloco máquina não vier): regex antigas
    procurando dentro do SCORE OPERACIONAL / setup mais próximo do score,
    nunca contagem de palavras no texto inteiro.
    """
    if not analysis:
        return "LONG", 50, "", [], "", ""

    tfs = timeframes_str.upper() if timeframes_str else ""
    tf_components = []
    style_match = RE_STYLE.search(analysis)
    if style_match:
        tf_components.append(style_match.group(1).upper())
    found_tfs = [tf for tf in TIMEFRAMES_MAP if tf in tfs]
    tf_components.extend(found_tfs)
    tf_label = " ".join(tf_components)

    # --- Tenta o bloco máquina primeiro (fonte de verdade) ---
    dm = RE_DIRECAO_FINAL.search(analysis)
    sm = RE_SCORE_FINAL.search(analysis)
    if dm and sm:
        direction = dm.group(1).upper()
        score = int(sm.group(1))
        if score > 100:
            score = 100
        entry_m = RE_ENTRY_FINAL.search(analysis)
        sl_m = RE_SL_FINAL.search(analysis)
        tp1_m = RE_TP1_FINAL.search(analysis)
        tp2_m = RE_TP2_FINAL.search(analysis)
        tp3_m = RE_TP3_FINAL.search(analysis)
        entry = entry_m.group(1).replace(',', '.') if entry_m else ""
        sl = sl_m.group(1).replace(',', '.') if sl_m else ""
        tps = []
        for m in (tp1_m, tp2_m, tp3_m):
            if m:
                tps.append(m.group(1).replace(',', '.'))
        return direction, score, sl, tps, tf_label, entry

    # --- Fallback antigo (bloco máquina não veio — não deveria acontecer,
    #     mas protege contra resposta fora do formato) ---
    sm_old = RE_SCORE.search(analysis)
    score = int(sm_old.group(1)) if sm_old else 50
    if score > 100:
        score = 100

    # Direção no fallback: olha só a janela de texto perto do SCORE OPERACIONAL
    # e da RECOMENDACAO FINAL, nunca o texto inteiro (evita pegar "LONG" dentro
    # de avisos tipo "LONG AQUI É SUICÍDIO").
    window = analysis
    if sm_old:
        start = max(0, sm_old.start() - 200)
        window = analysis[start:]
    if "RECOMENDACAO FINAL" in analysis.upper():
        idx = analysis.upper().find("RECOMENDACAO FINAL")
        window = analysis[idx:idx + 600]

    wl = window.lower()
    sell_count = sum(wl.count(w) for w in ['short', 'bearish', 'venda'])
    buy_count = sum(wl.count(w) for w in ['long', 'bullish', 'compra'])
    direction = "SHORT" if sell_count > buy_count else "LONG"

    setup1_block = analysis
    if "SETUP #2" in analysis:
        setup1_block = analysis.split("SETUP #2")[0]
    sl_match = RE_SL.search(setup1_block)
    sl = sl_match.group(1).replace(',', '.') if sl_match else ""
    tp_matches = RE_TP.findall(setup1_block)
    tps = [tp.replace(',', '.') for tp in tp_matches[:3]]
    entry_match = RE_ENTRY.search(setup1_block)
    entry = entry_match.group(1).replace(',', '.') if entry_match else ""

    return direction, score, sl, tps, tf_label, entry


def init_db():
    try:
        with sqlite3.connect(DB_FILE) as conn:
            cursor = conn.cursor()
            cursor.execute('''
                CREATE TABLE IF NOT EXISTS alerts (
                    id TEXT PRIMARY KEY,
                    pair TEXT,
                    target REAL,
                    analysis TEXT,
                    timeframes TEXT
                )
            ''')
            cursor.execute('''
                CREATE TABLE IF NOT EXISTS journal (
                    id TEXT PRIMARY KEY,
                    pair TEXT,
                    created_at INTEGER,
                    direction TEXT,
                    score INTEGER,
                    entry TEXT,
                    sl TEXT,
                    tp1 TEXT,
                    tp2 TEXT,
                    tp3 TEXT,
                    timeframes TEXT,
                    analysis TEXT,
                    status TEXT DEFAULT 'pending',
                    pnl REAL DEFAULT 0,
                    notes TEXT DEFAULT ''
                )
            ''')
            conn.commit()
        print("Base de dados SQLite inicializada com sucesso!")
    except Exception as e:
        print(f"Erro ao inicializar Base de Dados: {e}")


def send_telegram(message):
    try:
        url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
        requests.post(url, json={"chat_id": TELEGRAM_CHAT_ID, "text": message, "parse_mode": "HTML"}, timeout=10)
    except Exception as e:
        print(f"Telegram erro: {e}")


def check_alerts_inline():
    try:
        with sqlite3.connect(DB_FILE) as conn:
            cursor = conn.cursor()
            cursor.execute("SELECT id, pair, target, analysis, timeframes FROM alerts")
            db_alerts = cursor.fetchall()
        if not db_alerts:
            return
        for row in db_alerts:
            alert_id, pair, target, analysis, timeframes_str = row[0], row[1], row[2], row[3], row[4]
            if timeframes_str is None:
                timeframes_str = ""
            current_price = PRECOS_TICKER.get(pair)
            if current_price is None:
                continue
            distancia = abs(current_price - target)
            margem_tolerancia = target * 0.0015
            if distancia <= margem_tolerancia:
                with sqlite3.connect(DB_FILE) as conn_del:
                    cursor_del = conn_del.cursor()
                    cursor_del.execute("DELETE FROM alerts WHERE id = ?", (alert_id,))
                    linhas_afetadas = cursor_del.rowcount
                    conn_del.commit()
                if linhas_afetadas > 0:
                    direction, score, sl, tps, tf_label, entry = extract_trade_info(analysis, timeframes_str)
                    arrow = "📈" if direction == "LONG" else "📉"
                    emoji_score = "🟢" if score >= 75 else "🟡" if score >= 50 else "🔴"
                    msg = f"🎯 <b>{pair} ATINGIDO!</b>\n\n"
                    msg += f"{arrow} <b>{direction}</b> | {tf_label}\n"
                    msg += f"💰 <b>Preço Atual:</b> ${current_price:,.2f}\n"
                    msg += f"🎯 <b>Alvo atingido:</b> ${target:,.2f}\n"
                    msg += f"-------------------------------------\n"
                    if entry:
                        msg += f"📍 <b>Entrada Conservadora:</b> ${entry}\n"
                    if sl:
                        msg += f"🛑 <b>Stop Loss (SL):</b> ${sl}\n"
                    if tps:
                        for i, tp in enumerate(tps, 1):
                            msg += f"✅ <b>Take Profit {i} (TP{i}):</b> ${tp}\n"
                    else:
                        msg += f"✅ <b>Take Profit (TP):</b> N/A\n"
                    msg += f"-------------------------------------\n"
                    msg += f"{emoji_score} <b>Score Operacional:</b> {score}/100\n\n"
                    msg += f"💡 <i>Aguardar mitigação de FVG ou Order Block se aplicável.</i>"
                    send_telegram(msg)
    except Exception as e:
        print(f"Erro ao processar alertas inline: {e}")


init_db()

# ─── PROMPT ICT CACHEADO ─────────────────────────────────────────────────────
ICT_SYSTEM_PROMPT = (
    "Es um mentor institucional ICT (Inner Circle Trader) e SMC de elite, com "
    "os olhos e o raciocinio dos melhores traders profissionais do mundo. "
    "Zero tolerancia para analises vagas, ficticias ou com nivel de confianca "
    "inventado.\n\n"

    "COMO FALAR (TOM DE VOZ — OBRIGATORIO):\n"
    "- Fala como um trader senior explicando o grafico a um mentorado, nao "
    "como um relatorio ou formulario preenchido.\n"
    "- Cada camada de analise deve ser NARRADA em frases conectadas, "
    "explicando o raciocinio de uma para a outra — nao uma lista seca de "
    "campos.\n"
    "- Evita repetir a mesma estrutura robotica em cada linha. Varia a forma "
    "de comecar as frases.\n"
    "- Nao perdes nenhuma camada tecnica nem nenhum numero exato so para "
    "soar mais natural — o rigor tecnico e os valores exatos sao "
    "inegociaveis. Humanizar e na FORMA de contar, nao no conteudo.\n"
    "- Nunca uses frases vagas tipo 'na regiao de' ou 'por ai' — sempre "
    "preco exato visivel no grafico.\n\n"

    "PRINCIPIO FUNDAMENTAL — PROBABILIDADE, NUNCA PREVISAO:\n"
    "- Tu NUNCA prevês para onde o preco vai. Tu avalias PROBABILIDADE "
    "baseada em confluencia tecnica real, sempre com nivel de invalidacao "
    "(stop) definido.\n"
    "- Numeros escritos no grafico (RSI, MACD, preco, MAs) sao leitura de "
    "texto — reporta com certeza total, sem arredondar ou inventar.\n"
    "- Zonas estruturais (OB, FVG, suporte/resistencia, CHoCH) sao "
    "interpretacao tecnica competente, nao fato objetivo — trata como tal, "
    "mas com o mesmo rigor de um analista ICT senior.\n\n"

    "REGRA DE OURO ABSOLUTA:\n"
    "- NUNCA recomendar LONG quando o preco esta no topo do range diario ou em resistencia forte\n"
    "- NUNCA recomendar SHORT quando o preco esta no fundo do range diario ou em suporte forte\n"
    "- NUNCA inventar niveis — todos os valores devem ser visiveis nos graficos fornecidos\n"
    "- Se nao ha setup claro = dizer FORA DO MERCADO sem hesitar\n"
    "- Stop Loss SEMPRE atras de estrutura ICT real — NUNCA arbitrario\n"
    "- TP SEMPRE onde o mercado vai buscar liquidez real — BSL/SSL identificados\n\n"

    "RACIOCINIO EM CASCATA — OBRIGATORIO ANTES DAS 16 CAMADAS:\n"
    "Antes de entrar em qualquer camada isolada, narra o raciocinio "
    "descendo os timeframes disponiveis (D1 -> H4 -> H1 -> M15/M5), "
    "explicando o que cada timeframe mostra e por que isso importa para o "
    "proximo. Exemplo de espirito (nao copiar literalmente): 'No D1 vejo "
    "que o preco varreu a liquidez em X e reagiu, entao o bias vira Y. "
    "Descendo para H4, a estrutura confirma isso porque...'. So depois "
    "desta cascata entras nas 16 camadas detalhadas.\n\n"

    "ANALISE OBRIGATORIA - 16 CAMADAS ICT:\n"
    "1. HTF Narrative & Daily Bias (D1/W1 - tendencia macro)\n"
    "2. Liquidez Pendente (BSL e SSL com valores exatos)\n"
    "3. Premium vs Discount Zone (Fibonacci 50% — onde esta o preco agora)\n"
    "4. Order Blocks (OB bullish e bearish com zonas exatas por TF)\n"
    "5. Fair Value Gaps (FVG com zonas exatas e status: preenchido/aberto)\n"
    "6. CHoCH / MSS (confirmado ou potencial, com nivel exato)\n"
    "7. Liquidity Sweeps (varreduras recentes com valores exatos)\n"
    "8. Mitigation & Breaker Blocks\n"
    "9. Killzones & Session Patterns (Asia/London/NY — qual esta ativa)\n"
    "10. Midnight Open (valor exato e posicao do preco em relacao a ele — define bias intraday)\n"
    "11. OTE - Optimal Trade Entry (61.8%, 70.5%, 79% do swing criado pelo sweep)\n"
    "12. Wyckoff Phase (Acumulacao/Markup/Distribuicao/Markdown + Spring/UTAD)\n"
    "13. Power of Three - PO3/AMD (Accumulation/Manipulation/Distribution intraday)\n"
    "14. IFVG - Inversion Fair Value Gap (FVGs invertidos)\n"
    "15. Gatilhos de Continuidade/Reversao (ver secao propria abaixo)\n"
    "16. Divergencias RSI/MACD x preco (ver secao propria abaixo)\n\n"

    "CAMADA 15 — GATILHOS DE CONTINUIDADE/REVERSAO (regras de classificacao):\n"
    "- Order Block (OB) -> papel: CONTINUIDADE. Sempre reporta e sempre "
    "conta no score.\n"
    "- Fair Value Gap (FVG) -> papel: CONTINUIDADE. Sempre reporta e "
    "sempre conta no score.\n"
    "- Breaker Block -> papel: REVERSAO. So reporta e so conta no score se "
    "conseguires descrever a sequencia completa: (1) OB original, (2) "
    "rompimento do OB, (3) retorno do preco respeitando aquele nivel na "
    "direcao oposta. Se nao conseguires ver os 3 eventos com clareza, "
    "escreve 'Breaker Block nao confirmado com sequencia completa neste "
    "recorte' e NAO contas no score.\n"
    "- BPR (Balanced Price Range) -> exige 2 FVGs de polaridade oposta "
    "sobrepostos. So reporta se identificares com clareza os dois FVGs. "
    "Se identificares, reporta como informacao mas NAO conta no score "
    "(baixa confiabilidade de deteccao visual). Se nao identificares, "
    "escreve 'BPR nao identificavel com precisao neste recorte'.\n"
    "- IDM (Inducement) -> e interpretacao de intencao de mercado, nao "
    "geometria pura. Se identificares um candidato claro, reporta como "
    "informacao mas NAO conta no score. Se nao houver clareza, escreve "
    "'IDM nao identificavel com precisao neste recorte'.\n\n"

    "CAMADA 16 — DIVERGENCIAS (regra rigida, nao confundir com sobrecompra/sobrevenda):\n"
    "- Divergencia SO existe quando ha DOIS topos (ou dois fundos) "
    "comparaveis no PRECO, com o oscilador (RSI ou MACD) se movendo na "
    "direcao OPOSTA entre esses dois pontos.\n"
    "- RSI ou StochRSI sozinho em zona extrema (>70 ou <30) SEM um segundo "
    "topo/fundo para comparar NAO e divergencia — e apenas 'alerta de "
    "exaustao'. Reporta a diferenca explicitamente quando aplicavel: "
    "'sobrecompra extrema, mas sem segundo topo para confirmar divergencia' "
    "versus 'divergencia bearish confirmada: preco fez topo mais alto, RSI "
    "fez topo mais baixo'.\n"
    "- So conta no score como divergencia se a condicao dos dois topos/"
    "fundos comparaveis estiver satisfeita.\n\n"

    "CALCULO DO SCORE — DETERMINISTICO, NUNCA POR SENSACAO:\n"
    "O SCORE_FINAL (0-100) e resultado de somar o peso de cada camada que "
    "vota, nao uma impressao geral. Estrutura de pesos (soma normalizada "
    "para 100):\n"
    "- Bias D1/H4 alinhado com a direcao = +15\n"
    "- CHoCH/MSS confirmado na direcao = +15\n"
    "- Premium/Discount extremo (>70% ou <30% do range) a favor = +10\n"
    "- RSI/StochRSI sobrecomprado ou sobrevendido a favor = +10\n"
    "- MACD cruzamento confirmado a favor = +10\n"
    "- OB ativo na direcao = +10\n"
    "- FVG aberto na direcao = +10\n"
    "- Divergencia confirmada (regra rigida da Camada 16) a favor = +10\n"
    "- Breaker Block confirmado (sequencia completa) a favor = +5\n"
    "- ADR ja esgotado (>80% usado) contra novas entradas = -10 "
    "(penalizacao, nao soma para nenhum lado)\n"
    "Soma os pesos das camadas que se confirmaram na mesma direcao "
    "dominante. O resultado disso e o SCORE_FINAL. Se LONG e SHORT tiverem "
    "pesos parecidos e nenhum ultrapassar folga clara, a direcao e NEUTRO.\n\n"

    "ANALISE TECNICA OBRIGATORIA POR TIMEFRAME:\n"
    "Para cada TF disponivel (D1, H4, H1, M15, M5) identificar:\n"
    "- RSI: valor exato + sobrecomprado (>70) / sobrevendido (<30) / neutro\n"
    "- MACD: DIF vs DEA — cruzamento bullish/bearish + divergencia se existir (aplicar regra da Camada 16)\n"
    "- Estocástico: valor exato + zona de reversao potencial\n"
    "- ADR (Average Daily Range): calcular range medio diario e quanto JA foi usado hoje — se >80% do ADR usado = NAO ENTRAR na direcao do movimento\n\n"

    "TIPO DE SETUP — IDENTIFICAR SEMPRE:\n"
    "- TENDENCIA: pullback para OB/FVG na direcao do HTF\n"
    "- REVERSAO: sweep de liquidez + CHoCH confirmado\n"
    "- CONTINUIDADE: BOS confirmado + pullback para breaker/FVG\n\n"

    "NARRATIVA ICT COMPLETA:\n"
    "1. SWEEP: qual liquidez foi varrida, onde e quando\n"
    "2. CHoCH: confirmacao com nivel exato\n"
    "3. OTE: fibonacci do swing criado pelo sweep\n"
    "4. ENTRADA: retrace para 61.8%, 70.5% ou 79% com trigger exato\n"
    "5. GESTAO POS-ENTRADA: o que esperar depois\n"
    "6. PROXIMOS ALVOS: onde o mercado vai buscar liquidez\n"
    "7. CENARIOS ALTERNATIVOS: re-sweep, invalidacao, continuacao\n\n"

    "REGRAS DE ENTRADA:\n"
    "- Entrada SEMPRE em OB ou FVG — NUNCA fora dessas zonas\n"
    "- Stop SEMPRE atras de estrutura real — explicar PORQUE aquele nivel\n"
    "- Se BSL/SSL proximo do stop = AVISAR e alargar ou NAO entrar\n"
    "- D1 bearish + setup long = AVISO CRITICO obrigatorio\n"
    "- Minimo 3 confluencias ICT para entrada\n"
    "- Probabilidade: 3=60%, 4=70%, 5=80%, 6+=90%\n"
    "- Trigger obrigatorio: Engolfo Bullish/Bearish, Pin Bar, Inside Bar no M15\n\n"

    "BLOCO_DADOS — OBRIGATORIO NO FIM DA RESPOSTA, SEM EXCECAO:\n"
    "Depois de toda a analise narrada, termina SEMPRE com este bloco "
    "exatamente neste formato, em texto plano (sem tags HTML dentro do "
    "bloco), cada campo numa linha propria, com estes nomes de campo "
    "EXATOS (isto e lido por codigo, nao pode variar):\n"
    "BLOCO_DADOS_INICIO\n"
    "DIRECAO_FINAL: [LONG ou SHORT ou NEUTRO]\n"
    "SCORE_FINAL: [numero de 0 a 100]\n"
    "ENTRY_FINAL: [preco exato da entrada conservadora/intraday principal]\n"
    "SL_FINAL: [preco exato do stop loss dessa entrada]\n"
    "TP1_FINAL: [preco exato]\n"
    "TP2_FINAL: [preco exato]\n"
    "TP3_FINAL: [preco exato ou deixa em branco se nao aplicavel]\n"
    "BLOCO_DADOS_FIM\n"
    "Este bloco tem de ser 100 porcento consistente com a direcao e os "
    "precos discutidos no resto da resposta. Nunca contradizer.\n\n"

    "FORMATACAO:\n"
    "- NUNCA uses markdown (* # [ ])\n"
    "- Usa APENAS tags HTML: <b> <i> <u> no corpo da analise (fora do BLOCO_DADOS)\n"
    "- Fecha todas as tags HTML abertas\n"
)


def build_dynamic_prompt(pair, valid_tfs):
    return (
        f"Analisa os graficos de {pair} nos timeframes ({', '.join(valid_tfs)}) com maxima precisao e objetividade.\n\n"
        "FORMATO OBRIGATORIO DA RESPOSTA — SEGUIR EXATAMENTE:\n\n"

        "<b>⚡ KAIROS MENTOR — " + pair + "</b>\n"
        "Data/Hora: [data e hora UTC]\n\n"
        "---\n\n"

        "<b>🧭 RACIOCINIO EM CASCATA (D1 → H4 → H1 → M15/M5)</b>\n"
        "[narra o raciocinio descendo os timeframes, conectando o que cada "
        "um mostra e por que importa para o proximo, em tom de trader "
        "explicando, nao lista de campos]\n\n"
        "---\n\n"

        "<b>🧭 SENTIDO DO MERCADO</b>\n"
        "<b>Dominante:</b> [📈 LONG / 📉 SHORT / ⏸ NEUTRO — FORA]\n"
        "<b>Tipo de Setup:</b> [TENDENCIA / REVERSAO / CONTINUIDADE]\n"
        "<b>Proximo Passo Logico:</b> [1-2 frases diretas — o que o mercado vai fazer]\n"
        "<b>Midnight Open:</b> [valor exato] — preco esta [ACIMA/ABAIXO] — bias [BULLISH/BEARISH]\n"
        "<b>ADR Hoje:</b> [range ja usado hoje] de [ADR medio] — [% usado] — [ENTRADA OK / CUIDADO — range quase esgotado]\n\n"
        "---\n\n"

        "<b>📉 CENARIO SHORT — Probabilidade: [X]%</b>\n"
        "<b>Condicao:</b> [o que tem de acontecer para este cenario]\n\n"
        "<b>⚡ SCALP (M5/M15):</b>\n"
        "Short: $[valor] | SL: $[valor] | TP: $[valor] | RR: 1:[x]\n"
        "<b>Trigger:</b> [vela de confirmacao obrigatoria]\n\n"
        "<b>🕐 INTRADAY (H1):</b>\n"
        "Short: $[valor] | SL: $[valor] | TP1: $[valor] | TP2: $[valor] | RR: 1:[x]\n"
        "<b>Trigger:</b> [vela de confirmacao obrigatoria]\n\n"
        "<b>📅 SWING (H4/D1):</b>\n"
        "Short: $[valor] | SL: $[valor] | TP1: $[valor] | TP2: $[valor] | TP3: $[valor] | RR: 1:[x]\n"
        "<b>Trigger:</b> [vela de confirmacao obrigatoria]\n\n"
        "<b>😴 PASSIVO (ordem limite short):</b>\n"
        "Limit Short: $[valor] | SL: $[valor] | TP: $[valor] | RR: 1:[x]\n\n"
        "<b>⚠️ Invalida SHORT se:</b> [nivel exato]\n\n"
        "---\n\n"

        "<b>📈 CENARIO LONG — Probabilidade: [X]%</b>\n"
        "<b>Condicao:</b> [o que tem de acontecer para este cenario]\n\n"
        "<b>⚡ SCALP (M5/M15):</b>\n"
        "Long: $[valor] | SL: $[valor] | TP: $[valor] | RR: 1:[x]\n"
        "<b>Trigger:</b> [vela de confirmacao obrigatoria]\n\n"
        "<b>🕐 INTRADAY (H1):</b>\n"
        "Long: $[valor] | SL: $[valor] | TP1: $[valor] | TP2: $[valor] | RR: 1:[x]\n"
        "<b>Trigger:</b> [vela de confirmacao obrigatoria]\n\n"
        "<b>📅 SWING (H4/D1):</b>\n"
        "Long: $[valor] | SL: $[valor] | TP1: $[valor] | TP2: $[valor] | TP3: $[valor] | RR: 1:[x]\n"
        "<b>Trigger:</b> [vela de confirmacao obrigatoria]\n\n"
        "<b>😴 PASSIVO (ordem limite long):</b>\n"
        "Limit Long: $[valor] | SL: $[valor] | TP: $[valor] | RR: 1:[x]\n\n"
        "<b>⚠️ Invalida LONG se:</b> [nivel exato]\n\n"
        "---\n\n"

        "<b>📊 ANALISE COMPLETA — 16 CAMADAS ICT</b>\n\n"

        "<b>BIAS DE MERCADO</b>\n"
        "- <b>D1:</b> [BULLISH/BEARISH] — RSI:[valor] MACD:[bull/bear] Estoc:[valor]\n"
        "- <b>H4:</b> [BULLISH/BEARISH] — RSI:[valor] MACD:[bull/bear] Estoc:[valor]\n"
        "- <b>H1:</b> [bias] — RSI:[valor] MACD:[bull/bear] Estoc:[valor]\n"
        "- <b>M15/M5:</b> [bias] — RSI:[valor] MACD:[bull/bear] Estoc:[valor]\n\n"

        "<b>DIVERGENCIAS (Camada 16)</b>\n"
        "[para cada timeframe onde houver dois topos/fundos comparaveis, "
        "reporta se ha ou nao divergencia confirmada segundo a regra rigida. "
        "Onde nao houver segundo topo/fundo, reporta como 'alerta de "
        "exaustao', nunca como divergencia]\n\n"

        "<b>LIQUIDEZ & ESTRUTURA</b>\n"
        "- <b>BSL:</b> $[valor] — [contexto]\n"
        "- <b>SSL:</b> $[valor] — [contexto]\n"
        "- <b>AVISO LIQUIDEZ:</b> [BSL/SSL que pode varrer stop]\n"
        "- <b>OB Bearish:</b> [zona exata] — papel: CONTINUIDADE\n"
        "- <b>OB Bullish:</b> [zona exata] — papel: CONTINUIDADE\n"
        "- <b>FVG Aberto:</b> [zona e status] — papel: CONTINUIDADE\n"
        "- <b>IFVG:</b> [zona e status]\n"
        "- <b>Breaker Block:</b> [zona se sequencia completa confirmada, "
        "senao 'nao confirmado neste recorte'] — papel: REVERSAO\n"
        "- <b>BPR:</b> [zona se identificado, senao 'nao identificavel com "
        "precisao neste recorte'] — informativo, nao conta no score\n"
        "- <b>IDM:</b> [nivel se identificado, senao 'nao identificavel com "
        "precisao neste recorte'] — informativo, nao conta no score\n"
        "- <b>CHoCH:</b> [status e nivel exato]\n\n"

        "<b>OTE — OPTIMAL TRADE ENTRY</b>\n"
        "- Swing: [low] para [high] — Range: [x] pontos\n"
        "- 61.8%: $[valor]\n"
        "- 70.5%: $[valor]\n"
        "- 79.0%: $[valor]\n"
        "- Zona OTE ideal: $[valor] — $[valor]\n\n"

        "<b>WYCKOFF + PO3/AMD</b>\n"
        "- Fase Wyckoff: [fase atual]\n"
        "- PO3: Accumulation [zona] / Manipulation [nivel] / Distribution [em curso/aguarda]\n\n"

        "<b>SCORE OPERACIONAL: [X]/100</b>\n"
        "- Confluencias ativas (com peso de cada uma): [lista]\n"
        "- Penalizacoes: [lista]\n\n"

        "<b>RECOMENDACAO FINAL</b>\n"
        "[narrativa completa mas objetiva — maximo 6 linhas, tom de trader "
        "explicando a decisao, nao relatorio]\n\n"
        "---\n\n"

        "BLOCO_DADOS_INICIO\n"
        "DIRECAO_FINAL: [LONG ou SHORT ou NEUTRO]\n"
        "SCORE_FINAL: [numero]\n"
        "ENTRY_FINAL: [preco exato]\n"
        "SL_FINAL: [preco exato]\n"
        "TP1_FINAL: [preco exato]\n"
        "TP2_FINAL: [preco exato]\n"
        "TP3_FINAL: [preco exato ou vazio]\n"
        "BLOCO_DADOS_FIM"
    )


def analyze_single_pair(pair, images_by_tf):
    """Analisa um único par e retorna o resultado."""
    valid_tfs = [tf for tf, img in images_by_tf.items() if img and isinstance(img, dict) and img.get('base64')]
    if len(valid_tfs) < 2:
        return None, f"Par {pair} precisa de pelo menos 2 graficos"

    dynamic_prompt = build_dynamic_prompt(pair, valid_tfs)

    content = []
    for tf in valid_tfs:
        img = images_by_tf[tf]
        b64_data = img['base64']
        if "," in b64_data:
            b64_data = b64_data.split(",")[-1]
        b64_data = b64_data.strip().replace("\n", "").replace("\r", "")
        mime = img.get('mimeType', 'image/jpeg') or 'image/jpeg'
        content.append({"type": "text", "text": f"Grafico {tf}:"})
        content.append({
            "type": "image",
            "source": {"type": "base64", "media_type": mime, "data": b64_data}
        })
    content.append({"type": "text", "text": dynamic_prompt})

    response = client.messages.create(
        model="claude-sonnet-4-6",
        max_tokens=16000,
        temperature=0,
        system=[{
            "type": "text",
            "text": ICT_SYSTEM_PROMPT,
            "cache_control": {"type": "ephemeral"}
        }],
        messages=[{"role": "user", "content": content}]
    )

    raw_text = response.content[0].text

    # Remove o BLOCO_DADOS do texto que vai pro usuário ler (ele é só pra
    # extração via código — não faz sentido mostrar isso no app).
    display_text = raw_text
    if "BLOCO_DADOS_INICIO" in raw_text:
        display_text = raw_text.split("BLOCO_DADOS_INICIO")[0].rstrip()
        display_text = display_text.rstrip("-").rstrip()

    return raw_text, display_text, None


@app.route('/')
def index():
    return send_from_directory('.', 'index.html')


@app.route('/update_prices', methods=['POST'])
def update_prices():
    try:
        dados = request.json or {}
        for pair, price in dados.items():
            if price is not None:
                PRECOS_TICKER[pair] = float(price)
        check_alerts_inline()
        return jsonify({'ok': True, 'prices_stored': len(PRECOS_TICKER)})
    except Exception as e:
        return jsonify({'error': str(e)}), 400


# ─── ENDPOINT ANTIGO (um par) — mantido para compatibilidade ────────────────
@app.route('/analyze', methods=['POST'])
def analyze():
    try:
        data = request.json or {}
        pair = data.get('pair', 'BTCUSD')
        images = data.get('images', {})
        valid_tfs = [tf for tf, img in images.items() if img and isinstance(img, dict) and img.get('base64')]
        if len(valid_tfs) < 2:
            return jsonify({'error': 'Carrega pelo menos 2 graficos validos!'}), 400

        raw_text, display_text, error = analyze_single_pair(pair, images)
        if error:
            return jsonify({'error': error}), 400

        direction, score, sl, tps, tf_label, entry = extract_trade_info(raw_text, ','.join(valid_tfs))
        journal_id = f"{pair}_{int(time.time() * 1000)}"
        try:
            with sqlite3.connect(DB_FILE) as conn:
                cursor = conn.cursor()
                cursor.execute('''
                    INSERT INTO journal (id, pair, created_at, direction, score, entry, sl, tp1, tp2, tp3, timeframes, analysis, status)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ''', (
                    journal_id, pair, int(time.time()),
                    direction, score, entry, sl,
                    tps[0] if len(tps) > 0 else '',
                    tps[1] if len(tps) > 1 else '',
                    tps[2] if len(tps) > 2 else '',
                    ','.join(valid_tfs), raw_text, 'pending'
                ))
                conn.commit()
        except Exception as e:
            print(f"Erro ao guardar no journal: {e}")

        return jsonify({
            'result': display_text,
            'timeframes': ','.join(valid_tfs),
            'journal_id': journal_id,
            'score': score,
            'direction': direction,
            'entry': entry,
            'sl': sl,
            'tp1': tps[0] if len(tps) > 0 else '',
            'tp2': tps[1] if len(tps) > 1 else '',
            'tp3': tps[2] if len(tps) > 2 else '',
        })
    except Exception as e:
        return jsonify({'error': f"Erro na API Anthropic: {str(e)}"}), 500


# ─── ENDPOINT NOVO (multi-par) ───────────────────────────────────────────────
@app.route('/analyze_multi', methods=['POST'])
def analyze_multi():
    """
    Recebe vários pares de uma vez.
    Body: {
      "pairs": {
        "BNBUSD": { "D1": {base64, mimeType}, "H4": {...}, ... },
        "SOLUSD": { "H4": {...}, "M15": {...} },
        ...
      }
    }
    """
    try:
        data = request.json or {}
        pairs_data = data.get('pairs', {})

        if not pairs_data:
            return jsonify({'error': 'Nenhum par recebido'}), 400

        results = []
        errors = []

        for pair, images_by_tf in pairs_data.items():
            try:
                raw_text, display_text, error = analyze_single_pair(pair, images_by_tf)
                if error:
                    errors.append({'pair': pair, 'error': error})
                    continue

                valid_tfs = [tf for tf, img in images_by_tf.items() if img and isinstance(img, dict) and img.get('base64')]
                direction, score, sl, tps, tf_label, entry = extract_trade_info(raw_text, ','.join(valid_tfs))
                journal_id = f"{pair}_{int(time.time() * 1000)}"

                try:
                    with sqlite3.connect(DB_FILE) as conn:
                        cursor = conn.cursor()
                        cursor.execute('''
                            INSERT INTO journal (id, pair, created_at, direction, score, entry, sl, tp1, tp2, tp3, timeframes, analysis, status)
                            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                        ''', (
                            journal_id, pair, int(time.time()),
                            direction, score, entry, sl,
                            tps[0] if len(tps) > 0 else '',
                            tps[1] if len(tps) > 1 else '',
                            tps[2] if len(tps) > 2 else '',
                            ','.join(valid_tfs), raw_text, 'pending'
                        ))
                        conn.commit()
                except Exception as e:
                    print(f"Erro ao guardar journal {pair}: {e}")

                results.append({
                    'pair': pair,
                    'result': display_text,
                    'timeframes': ','.join(valid_tfs),
                    'journal_id': journal_id,
                    'score': score,
                    'direction': direction,
                    'entry': entry,
                    'sl': sl,
                    'tp1': tps[0] if len(tps) > 0 else '',
                    'tp2': tps[1] if len(tps) > 1 else '',
                    'tp3': tps[2] if len(tps) > 2 else '',
                })

            except Exception as e:
                errors.append({'pair': pair, 'error': str(e)})

        return jsonify({'results': results, 'errors': errors})

    except Exception as e:
        return jsonify({'error': f"Erro geral: {str(e)}"}), 500


@app.route('/set_alert', methods=['POST'])
def set_alert():
    try:
        data = request.json or {}
        pair = data.get('pair', 'BTCUSD')
        target = float(data.get('target'))
        analysis = data.get('analysis', '')
        timeframes = data.get('timeframes', '')
        current_price = PRECOS_TICKER.get(pair)
        if not current_price:
            current_price = float(data.get('current_price', 0))
        alert_unique_id = f"{pair}_{target}_{int(time.time() * 1000)}"
        with sqlite3.connect(DB_FILE) as conn:
            cursor = conn.cursor()
            cursor.execute(
                "INSERT INTO alerts (id, pair, target, analysis, timeframes) VALUES (?, ?, ?, ?, ?)",
                (alert_unique_id, pair, target, analysis, timeframes)
            )
            conn.commit()
        send_telegram(f"<b>Alerta Gravado para {pair}</b>\nAlvo: ${target:,.2f}\nPreco atual: ${current_price:,.2f}")
        return jsonify({'ok': True, 'current_price': current_price})
    except Exception as e:
        return jsonify({'error': str(e)}), 500


@app.route('/journal', methods=['GET'])
def get_journal():
    try:
        pair_filter = request.args.get('pair', '')
        with sqlite3.connect(DB_FILE) as conn:
            cursor = conn.cursor()
            if pair_filter:
                cursor.execute('SELECT id, pair, created_at, direction, score, entry, sl, tp1, tp2, tp3, timeframes, status, pnl FROM journal WHERE pair=? ORDER BY created_at DESC LIMIT 100', (pair_filter,))
            else:
                cursor.execute('SELECT id, pair, created_at, direction, score, entry, sl, tp1, tp2, tp3, timeframes, status, pnl FROM journal ORDER BY created_at DESC LIMIT 100')
            rows = cursor.fetchall()
        trades = []
        for r in rows:
            trades.append({
                'id': r[0], 'pair': r[1], 'created_at': r[2],
                'direction': r[3], 'score': r[4], 'entry': r[5],
                'sl': r[6], 'tp1': r[7], 'tp2': r[8], 'tp3': r[9],
                'timeframes': r[10], 'status': r[11], 'pnl': r[12]
            })
        return jsonify({'trades': trades})
    except Exception as e:
        return jsonify({'error': str(e)}), 500


@app.route('/journal/update', methods=['POST'])
def update_journal():
    try:
        data = request.json or {}
        trade_id = data.get('id')
        status = data.get('status')
        pnl = float(data.get('pnl', 0))
        with sqlite3.connect(DB_FILE) as conn:
            cursor = conn.cursor()
            cursor.execute('UPDATE journal SET status=?, pnl=? WHERE id=?', (status, pnl, trade_id))
            conn.commit()
        return jsonify({'ok': True})
    except Exception as e:
        return jsonify({'error': str(e)}), 500


@app.route('/journal/stats', methods=['GET'])
def journal_stats():
    try:
        with sqlite3.connect(DB_FILE) as conn:
            cursor = conn.cursor()
            cursor.execute('SELECT status, pnl, score, pair FROM journal WHERE status != "pending" AND status != "cancelled"')
            rows = cursor.fetchall()
        total_trades = len(rows)
        wins = sum(1 for r in rows if r[0] == 'win')
        losses = sum(1 for r in rows if r[0] == 'loss')
        total_pnl = sum(r[1] for r in rows)
        win_rate = round((wins / total_trades * 100), 1) if total_trades > 0 else 0
        score_brackets = {'75-100': {'w': 0, 'l': 0}, '60-74': {'w': 0, 'l': 0}, '50-59': {'w': 0, 'l': 0}}
        for r in rows:
            s = r[2]
            if s >= 75:
                bracket = '75-100'
            elif s >= 60:
                bracket = '60-74'
            else:
                bracket = '50-59'
            if r[0] == 'win':
                score_brackets[bracket]['w'] += 1
            else:
                score_brackets[bracket]['l'] += 1
        pair_stats = {}
        for r in rows:
            p = r[3]
            if p not in pair_stats:
                pair_stats[p] = {'w': 0, 'l': 0, 'pnl': 0}
            pair_stats[p]['pnl'] += r[1]
            if r[0] == 'win':
                pair_stats[p]['w'] += 1
            else:
                pair_stats[p]['l'] += 1
        return jsonify({
            'total_trades': total_trades, 'wins': wins, 'losses': losses,
            'total_pnl': round(total_pnl, 2), 'win_rate': win_rate,
            'score_brackets': score_brackets, 'pair_stats': pair_stats
        })
    except Exception as e:
        return jsonify({'error': str(e)}), 500
